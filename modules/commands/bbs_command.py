#!/usr/bin/env python3
"""
BBS (Bulletin Board System) command for the MeshCore Bot.

Provides store-and-forward private messaging keyed on FULL public keys, so
two nodes advertising the same name never share a mailbox and queued
messages survive a recipient rename (migration 34, ``bbs_inbox``).

Recipient resolution mirrors the MeshCore-BBS reference implementation:

  1. exact contact name (case-insensitive)
  2. public-key prefix — a token of 4+ hex characters
  3. partial (contains) name match

Sending keeps the two-step flow for name ambiguity while hex prefixes are
resolved in one step:

  Step 1 — search for a recipient:
    s <partial_name>             — show a numbered shortlist (with key prefixes)
    send <partial_name>          — same
    bbs s <partial_name>         — same

  Step 2 — confirm recipient and send:
    s <N> <message>              — send message to match N from the shortlist
    s <keyprefix> <message>      — hex prefixes resolve directly, no shortlist

  If the search returns exactly one match and a message is already provided
  in step 1 (e.g. "s Alice hello"), the message is sent immediately without
  a shortlist.

  bbs r                        — read your pending messages (DM only)
  bbs list                     — show count of pending messages (DM only)
  bbs whoami                   — your name, key prefix and pending count (DM only)

Reading commits the read marks only after every reply chunk went out on the
radio (two-phase delivery), so a failed send never silently drops messages.
The legacy name-keyed ``bbs_messages`` table is still served by ``bbs r``
and ``bbs list`` until its age purge empties it.

Private recipient notifications, aggregated per recipient (one DM carrying
the unread count, never one DM per stored message):

  * on arrival      — right after a successful store (``notify_on_arrival``)
  * +24h unread     — reminder stage (``notify_reminder_hours``, swept by a
                      scheduler job, migration 35 stage columns)
  * +73h unread     — final notice naming the purge age (``notify_final_hours``)

Stage DMs are only stamped in the database after they actually went out, so
an unreachable recipient is retried on the next sweep.
"""

import re
import time
from collections import deque
from datetime import datetime
from typing import Any

from ..models import MeshMessage
from .base_command import BaseCommand

# Default seconds to wait before sending another inbox notification to the same user.
NOTIFICATION_COOLDOWN_SECONDS = 300

# Default unread-message age (hours) for the stage-2 private reminder DM.
DEFAULT_REMINDER_HOURS = 24

# Default unread-message age (hours) for the stage-3 final notice, timed to
# leave the recipient a clear warning window before the 7-day age purge.
DEFAULT_FINAL_NOTICE_HOURS = 73

# Default minutes between two sweeps of the scheduler notification job.
DEFAULT_NOTIFY_CHECK_MINUTES = 30

# Maximum UTF-8 byte length for a single BBS read response before it is chunked into per-message lines.
_MAX_SINGLE_MESSAGE_BYTES = 150

# How long (seconds) a pending recipient shortlist remains valid before expiring.
_PENDING_SELECTION_TTL = 300

# Default number of BBS commands a single user may issue per minute (0 = no limit).
DEFAULT_RATE_LIMIT_PER_MINUTE = 10

# Shortest public-key prefix accepted for direct recipient resolution.
_KEY_PREFIX_MIN_CHARS = 4

# Hexadecimal token (public-key prefix candidate).
_HEX_TOKEN_RE = re.compile(r"^[0-9a-fA-F]{4,256}$")


class BBSCommand(BaseCommand):
    """Per-user BBS store-and-forward messaging, keyed on public keys.

    Users can leave messages for other users by node name or key prefix.
    Recipients are notified when they DM the bot and can retrieve
    their messages on demand.
    """

    # Plugin metadata
    name = "bbs"
    keywords = ["bbs", "s", "send"]
    description = (
        "BBS store-and-forward messaging. "
        "'s <name|keyprefix>' to find a recipient, 's <N> <msg>' to send, "
        "'bbs r' to read, 'bbs list' for count."
    )
    category = "basic"

    # Documentation
    short_description = "Store-and-forward BBS messages between mesh nodes"
    usage = "s <name> | s <N> <message> | s <keyprefix> <message> | bbs r | bbs list | bbs whoami"
    examples = [
        "s Tom",
        "s 2 Hey, are you around?",
        "bbs s Alice",
        "bbs s 1 Meet at noon.",
        "bbs s a1b2c3 Meet at noon.",
        "bbs r",
        "bbs list",
        "bbs whoami",
    ]
    parameters = [
        {"name": "name", "description": "Recipient name or public-key prefix (4+ hex) to look up"},
        {"name": "N", "description": "Number from the shortlist to select as recipient"},
        {"name": "message", "description": "Message text to store for the recipient"},
    ]

    def __init__(self, bot: Any) -> None:
        super().__init__(bot)
        self.enabled: bool = self.get_config_value(
            "BBS_Command", "enabled", fallback=True, value_type="bool"
        )
        self.max_messages_per_user: int = self.get_config_value(
            "BBS_Command", "max_messages_per_user", fallback=10, value_type="int"
        )
        self.max_message_age_days: int = self.get_config_value(
            "BBS_Command", "max_message_age_days", fallback=7, value_type="int"
        )
        self.max_messages_per_sender_per_day: int = self.get_config_value(
            "BBS_Command", "max_messages_per_sender_per_day", fallback=20, value_type="int"
        )
        self.rate_limit_per_minute: int = self.get_config_value(
            "BBS_Command", "rate_limit_per_minute", fallback=DEFAULT_RATE_LIMIT_PER_MINUTE, value_type="int"
        )
        self.notify_cooldown_seconds: float = self.get_config_value(
            "BBS_Command", "notify_cooldown_seconds", fallback=NOTIFICATION_COOLDOWN_SECONDS, value_type="float"
        )
        # Private recipient notifications (stages 1-3, aggregated per recipient)
        self.notify_on_arrival: bool = self.get_config_value(
            "BBS_Command", "notify_on_arrival", fallback=True, value_type="bool"
        )
        self.notify_reminder_hours: int = max(
            0,
            self.get_config_value(
                "BBS_Command", "notify_reminder_hours", fallback=DEFAULT_REMINDER_HOURS, value_type="int"
            ),
        )
        self.notify_final_hours: int = max(
            0,
            self.get_config_value(
                "BBS_Command", "notify_final_hours", fallback=DEFAULT_FINAL_NOTICE_HOURS, value_type="int"
            ),
        )
        # In-memory cooldown: sender key -> last notification timestamp
        self._notification_cooldowns: dict[str, float] = {}
        # In-memory pending recipient shortlists: sender key -> (candidates, expires_at)
        # Each candidate is a (name, public_key) tuple.
        self._pending_selections: dict[str, tuple[list[tuple[str, str]], float]] = {}
        # Sliding per-user command windows: sender key -> timestamps of recent commands
        self._rate_windows: dict[str, deque[float]] = {}

    # ------------------------------------------------------------------
    # BaseCommand interface
    # ------------------------------------------------------------------

    def can_execute(self, message: MeshMessage, skip_channel_check: bool = False) -> bool:
        if not self.enabled:
            return False
        return super().can_execute(message, skip_channel_check)

    def get_help_text(self, message: MeshMessage | None = None) -> str:
        return (
            "BBS store-and-forward messaging:\n"
            "  s <name>         — search contacts & show shortlist\n"
            "  s <N> <msg>      — send msg to match N from shortlist\n"
            "  s <hex4+> <msg>  — send to a contact by key prefix\n"
            "  bbs r            — read your pending messages (DM only)\n"
            "  bbs list         — count pending messages (DM only)\n"
            "  bbs whoami       — your name, key prefix & pending count (DM only)"
        )

    async def execute(self, message: MeshMessage) -> bool:
        """Route to the appropriate sub-handler based on keyword and subcommand."""
        content = message.content.strip()
        words = content.split(None, 2)
        if not words:
            return await self._handle_help(message)

        first = words[0].lower()

        if first in ("s", "send"):
            # Direct-send shorthand: s <name> <message>
            args = content[len(first):].strip()
            return await self._handle_send(message, args)

        if first == "bbs":
            rest = content[len("bbs"):].strip()
            if not rest:
                return await self._handle_help(message)
            sub_words = rest.split(None, 1)
            sub = sub_words[0].lower()
            sub_args = sub_words[1] if len(sub_words) > 1 else ""

            if sub in ("s", "send", "msg"):
                return await self._handle_send(message, sub_args)
            if sub in ("r", "read"):
                return await self._handle_read(message)
            if sub in ("l", "list"):
                return await self._handle_list(message)
            if sub in ("w", "whoami"):
                return await self._handle_whoami(message)
            if sub == "help":
                return await self._handle_help(message)

            # Unknown subcommand — show help
            return await self._handle_help(message)

        return await self._handle_help(message)

    # ------------------------------------------------------------------
    # Sub-handlers
    # ------------------------------------------------------------------

    async def _handle_send(self, message: MeshMessage, args: str) -> bool:
        """Handle: [bbs] s|send|msg <name_or_number_or_keyprefix> [message]

        Two-step flow
        -------------
        Step 1 — search:
            s <query>
            Looks up *query* in known contacts: exact name first, then (for
            tokens of 4+ hex characters) full public keys starting with the
            token, then partial name matches.  If multiple matches are found
            a numbered shortlist (max 5, each shown with its key prefix) is
            returned and the selection is kept in memory for
            ``_PENDING_SELECTION_TTL`` seconds.  If there is exactly one
            match *and* a message was already provided, the message is sent
            immediately (no shortlist needed).

        Step 2 — confirm and send:
            s <N> <message>
            *N* is the number the user chose from the shortlist.  The pending
            selection for this sender is consumed and the message is stored.
        """
        if not args:
            await self.send_response(message, "Usage: s <name> | s <N> <message>")
            return True

        parts = args.split(None, 1)
        first = parts[0]
        rest = parts[1] if len(parts) > 1 else ""

        sender_key = self._sender_key(message)
        sender_name = message.sender_id or "unknown"

        if not self._rate_limit_ok(sender_key):
            await self.send_response(message, "Too many BBS commands — wait a minute.")
            return True

        # ------------------------------------------------------------------
        # Step 2: the user is selecting from a pending shortlist
        # ------------------------------------------------------------------
        if first.isdigit():
            candidates = self._get_pending_selection(sender_key)
            if candidates is None:
                await self.send_response(
                    message,
                    "No pending recipient list. Send 's <name>' to search.",
                )
                return True

            idx = int(first)
            if not (1 <= idx <= len(candidates)):
                await self.send_response(
                    message,
                    f"Pick a number between 1 and {len(candidates)}.",
                )
                return True

            if not rest:
                await self.send_response(message, f"Usage: s {idx} <message>")
                return True

            recipient = candidates[idx - 1]
            self._clear_pending_selection(sender_key)

        # ------------------------------------------------------------------
        # Step 1: resolve a recipient (exact name, key prefix, partial name)
        # ------------------------------------------------------------------
        else:
            matches = self._resolve_recipient(first)
            if not matches:
                await self.send_response(
                    message, f"No contact found matching '{first}'."
                )
                return True

            if len(matches) == 1 and rest:
                # Single unambiguous match with message already present — send directly.
                recipient = matches[0]
            else:
                # Multiple matches, or single match without a message → show shortlist.
                self._set_pending_selection(sender_key, matches)
                lines = "\n".join(
                    f"{i + 1} {self._format_candidate(name, key)}"
                    for i, (name, key) in enumerate(matches)
                )
                await self.send_response(message, f"{lines}\nReply: s <N> <message>")
                return True

        recipient_name, recipient_key = recipient

        # Self-send guard: public keys are authoritative; tolerate the
        # prefix-vs-full-key mismatch when a pubkey could not be resolved.
        if recipient_key and sender_key and (
            recipient_key == sender_key
            or recipient_key.startswith(sender_key)
            or sender_key.startswith(recipient_key)
        ):
            await self.send_response(message, "You cannot send a message to yourself.")
            return True

        # Store the confirmed message
        self._purge_old_messages()
        status = self._store_message(sender_key, sender_name, recipient_key, recipient_name, rest)
        if status == "ok":
            await self.send_response(message, f"Message stored for {recipient_name}.")
            await self._notify_recipient_arrival(recipient_key, recipient_name)
        elif status == "full":
            await self.send_response(
                message,
                f"Could not store message for {recipient_name} (mailbox full).",
            )
        elif status == "quota":
            await self.send_response(
                message,
                "Daily BBS quota reached. Try again tomorrow.",
            )
        else:
            await self.send_response(
                message,
                f"Could not store message for {recipient_name}.",
            )
        return True

    async def _handle_read(self, message: MeshMessage) -> bool:
        """Handle: bbs r|read — read pending messages (DM only for privacy).

        Two-phase delivery: unread rows are marked read only after every
        reply chunk went out on the radio, so a failed send leaves the
        messages unread for the next attempt instead of dropping them.
        """
        if not message.is_dm:
            await self.send_response(message, "BBS read is only available via DM.")
            return True

        sender_name = message.sender_id
        if not sender_name:
            await self.send_response(message, "Unable to identify sender.")
            return True

        sender_key = self._sender_key(message)

        new_rows = self._get_pending_messages_by_key(sender_key)
        legacy_rows = self._get_legacy_pending_messages(sender_name)
        if not new_rows and not legacy_rows:
            await self.send_response(message, "No pending BBS messages.")
            return True

        # Build per-message strings
        lines: list[str] = []
        for row in new_rows:
            lines.append(self._format_inbox_line(row[1], row[3], row[2]))
        for row in legacy_rows:
            lines.append(self._format_inbox_line(row[1], row[3], row[2]))

        # Send chunked if responses won't fit in one message
        all_sent = True
        full_response = "\n".join(lines)
        if len(full_response.encode("utf-8")) > _MAX_SINGLE_MESSAGE_BYTES:
            for line in lines:
                if not await self.send_response(message, line, skip_user_rate_limit=True):
                    all_sent = False
                    break
        else:
            all_sent = await self.send_response(message, full_response)

        if not all_sent:
            # Nothing was committed — the user can retry without losing messages.
            await self.send_response(
                message, "Send failed — messages kept. Retry 'bbs r'."
            )
            return True

        self._mark_messages_read(sender_key, sender_name)
        return True

    async def _handle_list(self, message: MeshMessage) -> bool:
        """Handle: bbs l|list — count pending messages (DM only)."""
        if not message.is_dm:
            await self.send_response(message, "BBS list is only available via DM.")
            return True

        sender_name = message.sender_id
        if not sender_name:
            await self.send_response(message, "Unable to identify sender.")
            return True

        sender_key = self._sender_key(message)
        count = self._get_pending_count(sender_key, sender_name)
        if count == 0:
            await self.send_response(message, "No pending BBS messages.")
        else:
            await self.send_response(
                message,
                f"You have {count} pending BBS message(s). Send 'bbs r' to read.",
            )
        return True

    async def _handle_whoami(self, message: MeshMessage) -> bool:
        """Handle: bbs w|whoami — identity the BBS uses for this user (DM only)."""
        if not message.is_dm:
            await self.send_response(message, "BBS whoami is only available via DM.")
            return True

        sender_name = message.sender_id or "unknown"
        sender_key = self._sender_key(message)
        count = self._get_pending_count(sender_key, sender_name)

        parts = [f"You are [{sender_name}]"]
        if sender_key and sender_key != sender_name:
            parts.append(f"key {sender_key[:8]}")
        parts.append(f"{count} pending")
        await self.send_response(message, " | ".join(parts))
        return True

    async def _handle_help(self, message: MeshMessage) -> bool:
        """Handle: bbs | bbs help — show usage."""
        await self.send_response(message, self.get_help_text())
        return True

    # ------------------------------------------------------------------
    # Inbox notification (called from message_handler for every inbound DM)
    # ------------------------------------------------------------------

    async def check_inbox_notification(self, message: MeshMessage) -> None:
        """Notify the user if they have pending BBS messages.

        Called from MessageHandler.process_message for every inbound DM.
        Skips notification if the DM already triggered a BBS command (the
        command replies for itself) or if we notified this user recently.

        Args:
            message: The inbound DM message.
        """
        if not self.enabled or not message.is_dm:
            return

        # Don't notify when the DM is itself a BBS command — keyword dispatch
        # already routes it to this command, which sends its own reply.
        content_lower = message.content.strip().lower()
        if self._is_bbs_command_message(content_lower):
            return

        sender_name = message.sender_id
        sender_key = self._sender_key(message)
        if not sender_name and not sender_key:
            return

        # Enforce per-user notification cooldown
        now = time.time()
        last_notified = self._notification_cooldowns.get(sender_key, 0.0)
        if now - last_notified < self.notify_cooldown_seconds:
            return

        count = self._get_pending_count(sender_key, sender_name)
        if count > 0:
            self._notification_cooldowns[sender_key] = now
            notification = (
                f"\U0001f4ec You have {count} pending BBS message(s). "
                "Send 'bbs r' to read."
            )
            try:
                await self.bot.command_manager.send_response(
                    message, notification, skip_user_rate_limit=True
                )
            except Exception as e:
                self.logger.error(f"Error sending BBS inbox notification: {e}")

    @staticmethod
    def _is_bbs_command_message(content_lower: str) -> bool:
        """Return True if the message dispatches to a BBS subcommand itself."""
        first = content_lower.split(None, 1)[0] if content_lower else ""
        return first in ("bbs", "s", "send")

    # ------------------------------------------------------------------
    # Staged private notifications (DMs to the recipient, aggregated)
    # ------------------------------------------------------------------

    async def _notify_recipient_arrival(self, recipient_key: str, recipient_name: str) -> None:
        """Stage 1: DM the recipient right after a message is stored.

        Aggregated: one DM carrying the recipient's total unread count, not
        one per stored message.  ``_notification_cooldowns`` (shared with the
        inbound-DM inbox reminder) keeps a burst of messages from turning into
        a burst of radio DMs; a message swallowed by the cooldown is still
        covered by the 24h reminder stage.  A failed notification never
        affects the store itself.
        """
        if not self.notify_on_arrival or not recipient_key:
            return
        now = time.time()
        if now - self._notification_cooldowns.get(recipient_key, 0.0) < self.notify_cooldown_seconds:
            return
        count = self._get_pending_count(recipient_key, recipient_name)
        if count <= 0:
            return
        try:
            sent = await self.bot.command_manager.send_dm(
                recipient_key, self._arrival_text(count), skip_user_rate_limit=True
            )
        except Exception as e:
            self.logger.error(f"Error sending BBS arrival notification: {e}")
            return
        if sent:
            self._notification_cooldowns[recipient_key] = now
        else:
            self.logger.info(
                f"BBS arrival notification not sent for {recipient_name} "
                f"(recipient not reachable as a contact)"
            )

    async def run_pending_notification_job(self) -> dict[str, Any]:
        """Stages 2 and 3: sweep the unread inbox and send age-based DMs.

        Called periodically by the scheduler (see
        ``MessageScheduler._setup_bbs_notification_job``).  For each stage —
        ``notify_reminder_hours`` (default 24h) and ``notify_final_hours``
        (default 73h, wording names the purge age) — recipients holding at
        least one unread message older than the stage threshold get one
        aggregate DM, and the stage column is stamped only on rows whose DM
        actually went out.  A recipient whose DM failed is retried on the
        next sweep; after ``max_message_age_days`` the purge wins and the
        stage is moot.

        Returns a summary dict: {'sent': int, 'failed': int}.
        """
        summary: dict[str, Any] = {"sent": 0, "failed": 0}
        stages = (
            ("reminder_sent_at", self.notify_reminder_hours, self._reminder_text),
            ("final_notice_sent_at", self.notify_final_hours, self._final_notice_text),
        )
        for stage_column, hours, text_fn in stages:
            if hours <= 0:
                continue
            for recipient_key, recipient_name, count in self._due_notification_recipients(stage_column, hours):
                try:
                    sent = await self.bot.command_manager.send_dm(
                        recipient_key, text_fn(count), skip_user_rate_limit=True
                    )
                except Exception as e:
                    self.logger.error(f"Error sending BBS {stage_column} notice: {e}")
                    summary["failed"] += 1
                    continue
                if not sent:
                    summary["failed"] += 1
                    self.logger.info(
                        f"BBS {stage_column} notice not sent for {recipient_name} "
                        f"(recipient not reachable as a contact)"
                    )
                    continue
                self._mark_notification_stage(recipient_key, stage_column)
                summary["sent"] += 1
                self.logger.info(
                    f"BBS {stage_column} notice sent for {recipient_name} ({count} unread)"
                )
        return summary

    def _arrival_text(self, count: int) -> str:
        """Stage 1 DM body."""
        return (
            f"\U0001f4ec New BBS message: you have {count} pending. "
            "Send 'bbs r' to read."
        )

    def _reminder_text(self, count: int) -> str:
        """Stage 2 (24h) DM body."""
        return (
            f"\U0001f4ec BBS reminder: you still have {count} unread message(s). "
            "Send 'bbs r' to read."
        )

    def _final_notice_text(self, count: int) -> str:
        """Stage 3 (73h) DM body — names the age purge so it reads as final."""
        return (
            f"\U0001f4ec BBS final notice: your {count} unread message(s) are deleted "
            f"after {self.max_message_age_days} days. Send 'bbs r' now."
        )

    def _due_notification_recipients(
        self, stage_column: str, hours: int
    ) -> list[tuple[str, str, int]]:
        """Recipients due for a *stage_column* DM: (key, name, unread_count).

        A recipient is due when at least one of their unstamped unread rows
        has aged past *hours*; the returned count is every unstamped unread
        row for that recipient, so the DM text and ``_mark_notification_stage``
        cover exactly the same set — including younger messages that joined
        the mailbox since the oldest crossed the threshold.  Newly stored
        rows carry a NULL stage column and age into a later sweep on their
        own.
        """
        if hours <= 0 or stage_column not in ("reminder_sent_at", "final_notice_sent_at"):
            return []
        try:
            with self.bot.db_manager.connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    f"SELECT recipient_key, MAX(recipient_name), COUNT(*) "
                    f"FROM bbs_inbox "
                    f"WHERE read_at IS NULL AND {stage_column} IS NULL "
                    f"GROUP BY recipient_key "
                    f"HAVING SUM(CASE WHEN sent_at <= datetime('now', ?) THEN 1 ELSE 0 END) > 0",
                    (f"-{int(hours)} hours",),
                )
                return [(row[0], row[1] or row[0], row[2]) for row in cursor.fetchall()]
        except Exception as e:
            self.logger.error(f"Error querying BBS notification stages: {e}")
            return []

    def _mark_notification_stage(self, recipient_key: str, stage_column: str) -> int:
        """Stamp *stage_column* on this recipient's still-unread rows.

        Only unread rows lacking the stamp are touched, so the DM matches
        what the sweep counted and rows already stamped are left alone.
        Returns the number of rows updated.
        """
        if stage_column not in ("reminder_sent_at", "final_notice_sent_at"):
            return 0
        try:
            with self.bot.db_manager.connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    f"UPDATE bbs_inbox SET {stage_column} = CURRENT_TIMESTAMP "
                    f"WHERE recipient_key = ? AND read_at IS NULL AND {stage_column} IS NULL",
                    (recipient_key,),
                )
                conn.commit()
                return cursor.rowcount
        except Exception as e:
            self.logger.error(f"Error stamping BBS notification stage: {e}")
            return 0

    # ------------------------------------------------------------------
    # Per-user sliding rate limit
    # ------------------------------------------------------------------

    def _rate_limit_ok(self, sender_key: str) -> bool:
        """Allow the command and remember it, or reject when the window is full."""
        if self.rate_limit_per_minute <= 0:
            return True
        now = time.monotonic()
        window = self._rate_windows.get(sender_key)
        if window is None:
            window = deque()
            self._rate_windows[sender_key] = window
        while window and now - window[0] >= 60.0:
            window.popleft()
        if len(window) >= self.rate_limit_per_minute:
            return False
        window.append(now)
        self._prune_rate_windows(now)
        return True

    def _prune_rate_windows(self, now: float) -> None:
        """Drop stale windows so the in-memory map cannot grow without bound."""
        if len(self._rate_windows) <= 200:
            return
        stale = [key for key, window in self._rate_windows.items() if not window or now - window[-1] >= 120.0]
        for key in stale:
            self._rate_windows.pop(key, None)

    # ------------------------------------------------------------------
    # Contact lookup and recipient resolution
    # ------------------------------------------------------------------

    @staticmethod
    def _sender_key(message: MeshMessage) -> str:
        """Identity key for the sender: full public key when resolved, else name."""
        return message.sender_pubkey or message.sender_id or "unknown"

    @staticmethod
    def _format_candidate(name: str, public_key: str) -> str:
        """Shortlist line with the key prefix that disambiguates same-name nodes."""
        if public_key:
            return f"{name} [{public_key[:_KEY_PREFIX_MIN_CHARS]}]"
        return name

    def _resolve_recipient(self, query: str) -> list[tuple[str, str]]:
        """Resolve *query* to candidate contacts: exact name → key prefix → partial name.

        Returns a list of (name, public_key) tuples, at most 5, deduplicated
        by public key.  A hex token is only resolved against public keys;
        otherwise name lookup wins.
        """
        exact = self._lookup_contacts(query, match="exact")
        if exact:
            return exact

        if _HEX_TOKEN_RE.match(query):
            by_key = self._lookup_contacts(query, match="key_prefix")
            if by_key:
                return by_key

        return self._lookup_contacts(query, match="partial")

    def _lookup_contacts(self, query: str, match: str = "partial") -> list[tuple[str, str]]:
        """Return up to 5 known companion contacts (name, public_key) for *query*.

        *match* selects the lookup mode: 'exact' (full name), 'key_prefix'
        (public keys starting with the token) or 'partial' (name contains).
        Queries the ``complete_contact_tracking`` table.  Returns an empty
        list when that table does not yet exist (e.g. fresh install before
        migrations have run) or when no contacts match.
        """
        if not query:
            return []
        try:
            with self.bot.db_manager.connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type='table' AND name='complete_contact_tracking'"
                )
                if not cursor.fetchone():
                    return []
                if match == "exact":
                    clause, param = "name = ? COLLATE NOCASE", query
                elif match == "key_prefix":
                    clause, param = "public_key LIKE ? COLLATE NOCASE", f"{query}%"
                else:
                    clause, param = "name LIKE ? COLLATE NOCASE", f"%{query}%"
                cursor.execute(
                    f"SELECT name, public_key FROM complete_contact_tracking "
                    f"WHERE {clause} AND role = 'companion' "
                    "ORDER BY last_heard DESC LIMIT 10",
                    (param,),
                )
                seen: set[str] = set()
                results: list[tuple[str, str]] = []
                for name, public_key in cursor.fetchall():
                    key = public_key or ""
                    if key and key in seen:
                        continue
                    seen.add(key)
                    results.append((name, key))
                    if len(results) >= 5:
                        break
                return results
        except Exception as e:
            self.logger.error(f"Error looking up BBS contacts: {e}")
            return []

    # ------------------------------------------------------------------
    # Pending recipient-selection state
    # ------------------------------------------------------------------

    def _get_pending_selection(self, sender_key: str) -> list[tuple[str, str]] | None:
        """Return the unexpired candidate list for *sender_key*, or ``None``."""
        entry = self._pending_selections.get(sender_key)
        if entry is None:
            return None
        candidates, expires_at = entry
        if time.time() > expires_at:
            self._pending_selections.pop(sender_key, None)
            return None
        return candidates

    def _set_pending_selection(self, sender_key: str, candidates: list[tuple[str, str]]) -> None:
        """Store *candidates* for *sender_key* with a TTL of ``_PENDING_SELECTION_TTL``."""
        self._pending_selections[sender_key] = (
            candidates,
            time.time() + _PENDING_SELECTION_TTL,
        )

    def _clear_pending_selection(self, sender_key: str) -> None:
        """Remove the pending selection for *sender_key* (no-op if absent)."""
        self._pending_selections.pop(sender_key, None)

    # ------------------------------------------------------------------
    # Database helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _format_inbox_line(sender_name: Any, sent_at: Any, text: Any) -> str:
        """Render one stored message as 'From <name> [MM/DD HH:MM]: <text>'."""
        try:
            dt = datetime.fromisoformat(str(sent_at))
            date_str = dt.strftime("%m/%d %H:%M")
        except Exception:
            date_str = str(sent_at)[:10]
        return f"From {sender_name} [{date_str}]: {text}"

    def _store_message(
        self,
        sender_key: str,
        sender_name: str,
        recipient_key: str,
        recipient_name: str,
        message_text: str,
    ) -> str:
        """Store a BBS message for *recipient_key*.

        Returns 'ok' on success, 'full' if the recipient mailbox is at
        capacity, 'quota' if the sender hit their daily limit, or 'error'.
        All limits are keyed on public keys, not display names.
        """
        try:
            with self.bot.db_manager.connection() as conn:
                cursor = conn.cursor()

                # Check recipient mailbox capacity
                cursor.execute(
                    "SELECT COUNT(*) FROM bbs_inbox "
                    "WHERE recipient_key = ? AND read_at IS NULL",
                    (recipient_key,),
                )
                count = cursor.fetchone()[0]
                if self.max_messages_per_user > 0 and count >= self.max_messages_per_user:
                    self.logger.warning(
                        f"BBS mailbox full for {recipient_name} ({count} unread)"
                    )
                    return "full"

                # Check per-sender daily quota (0 = unlimited)
                if self.max_messages_per_sender_per_day > 0:
                    cursor.execute(
                        "SELECT COUNT(*) FROM bbs_inbox "
                        "WHERE sender_key = ? "
                        "AND date(sent_at) = date('now')",
                        (sender_key,),
                    )
                    sent_today = cursor.fetchone()[0]
                    if sent_today >= self.max_messages_per_sender_per_day:
                        self.logger.warning(
                            f"BBS daily quota reached for sender {sender_name} "
                            f"({sent_today}/{self.max_messages_per_sender_per_day})"
                        )
                        return "quota"

                conn.execute(
                    "INSERT INTO bbs_inbox "
                    "(sender_key, sender_name, recipient_key, recipient_name, message) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (sender_key, sender_name, recipient_key, recipient_name, message_text),
                )
                conn.commit()
                self.logger.info(
                    f"BBS: {sender_name} -> {recipient_name}: {message_text[:50]}"
                )
                return "ok"
        except Exception as e:
            self.logger.error(f"Error storing BBS message: {e}")
            return "error"

    def _get_pending_count(self, recipient_key: str, recipient_name: str | None) -> int:
        """Number of unread messages waiting for this user (new + legacy tables)."""
        total = 0
        try:
            with self.bot.db_manager.connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT COUNT(*) FROM bbs_inbox "
                    "WHERE recipient_key = ? AND read_at IS NULL",
                    (recipient_key,),
                )
                total += cursor.fetchone()[0]
        except Exception as e:
            self.logger.error(f"Error counting BBS messages: {e}")
        total += self._get_legacy_pending_count(recipient_name)
        return total

    def _get_pending_messages_by_key(self, recipient_key: str) -> list:
        """Return unread bbs_inbox messages for *recipient_key*, oldest first."""
        if not recipient_key:
            return []
        try:
            with self.bot.db_manager.connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT id, sender_name, message, sent_at "
                    "FROM bbs_inbox "
                    "WHERE recipient_key = ? AND read_at IS NULL "
                    "ORDER BY sent_at ASC, id ASC",
                    (recipient_key,),
                )
                return cursor.fetchall()
        except Exception as e:
            self.logger.error(f"Error fetching BBS messages: {e}")
            return []

    def _mark_messages_read(self, recipient_key: str, recipient_name: str) -> int:
        """Mark all unread messages for this user as read in both tables.

        Returns the number of rows updated.
        """
        updated = 0
        try:
            with self.bot.db_manager.connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "UPDATE bbs_inbox "
                    "SET read_at = CURRENT_TIMESTAMP "
                    "WHERE recipient_key = ? AND read_at IS NULL",
                    (recipient_key,),
                )
                updated += cursor.rowcount
                conn.commit()
        except Exception as e:
            self.logger.error(f"Error marking BBS messages as read: {e}")
        try:
            with self.bot.db_manager.connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "UPDATE bbs_messages "
                    "SET read_at = CURRENT_TIMESTAMP "
                    "WHERE recipient_name = ? COLLATE NOCASE AND read_at IS NULL",
                    (recipient_name,),
                )
                updated += cursor.rowcount
                conn.commit()
        except Exception as e:
            self.logger.debug(f"Error marking legacy BBS messages as read: {e}")
        return updated

    # --- Legacy (name-keyed bbs_messages) helpers — read-only paths ---

    def _legacy_table_exists(self, conn: Any) -> bool:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='bbs_messages'"
        )
        return cursor.fetchone() is not None

    def _get_legacy_pending_count(self, recipient_name: str | None) -> int:
        """Return the number of unread legacy messages for *recipient_name*."""
        if not recipient_name:
            return 0
        try:
            with self.bot.db_manager.connection() as conn:
                if not self._legacy_table_exists(conn):
                    return 0
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT COUNT(*) FROM bbs_messages "
                    "WHERE recipient_name = ? COLLATE NOCASE AND read_at IS NULL",
                    (recipient_name,),
                )
                return cursor.fetchone()[0]
        except Exception as e:
            self.logger.error(f"Error counting legacy BBS messages: {e}")
            return 0

    def _get_legacy_pending_messages(self, recipient_name: str) -> list:
        """Return all unread legacy messages for *recipient_name*, oldest first."""
        if not recipient_name:
            return []
        try:
            with self.bot.db_manager.connection() as conn:
                if not self._legacy_table_exists(conn):
                    return []
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT id, sender_name, message, sent_at "
                    "FROM bbs_messages "
                    "WHERE recipient_name = ? COLLATE NOCASE AND read_at IS NULL "
                    "ORDER BY sent_at ASC",
                    (recipient_name,),
                )
                return cursor.fetchall()
        except Exception as e:
            self.logger.error(f"Error fetching legacy BBS messages: {e}")
            return []

    def _purge_old_messages(self) -> None:
        """Delete messages older than *max_message_age_days* from both tables."""
        try:
            with self.bot.db_manager.connection() as conn:
                conn.execute(
                    "DELETE FROM bbs_inbox "
                    "WHERE sent_at < datetime('now', ? || ' days')",
                    (f"-{self.max_message_age_days}",),
                )
                if self._legacy_table_exists(conn):
                    conn.execute(
                        "DELETE FROM bbs_messages "
                        "WHERE sent_at < datetime('now', ? || ' days')",
                        (f"-{self.max_message_age_days}",),
                    )
                conn.commit()
        except Exception as e:
            self.logger.debug(f"Error purging old BBS messages: {e}")
