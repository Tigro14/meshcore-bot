#!/usr/bin/env python3
"""
BBS (Bulletin Board System) command for the MeshCore Bot.

Provides per-user store-and-forward messaging so nodes can leave messages
for other nodes to read when they next contact the bot.

Sending uses a two-step flow to handle node names that contain spaces or
special characters:

  Step 1 — search for a recipient:
    s <partial_name>             — show a numbered shortlist of matching contacts
    send <partial_name>          — same
    bbs s <partial_name>         — same

  Step 2 — confirm recipient and send:
    s <N> <message>              — send message to match N from the shortlist
    send <N> <message>           — same
    bbs s <N> <message>          — same

  If the search returns exactly one match and a message is already provided
  in step 1 (e.g. "s Alice hello"), the message is sent immediately without
  a shortlist.

  bbs r                        — read your pending messages (DM only)
  bbs list                     — show count of pending messages (DM only)
"""

import time
from datetime import datetime
from typing import Any

from ..models import MeshMessage
from .base_command import BaseCommand

# How long (seconds) to wait before sending another inbox notification to the same user.
NOTIFICATION_COOLDOWN_SECONDS = 300

# Maximum UTF-8 byte length for a single BBS read response before it is chunked into per-message lines.
_MAX_SINGLE_MESSAGE_BYTES = 150

# How long (seconds) a pending recipient shortlist remains valid before expiring.
_PENDING_SELECTION_TTL = 300


class BBSCommand(BaseCommand):
    """Per-user BBS store-and-forward messaging.

    Users can leave messages for other users by node name.
    Recipients are notified when they DM the bot and can retrieve
    their messages on demand.
    """

    # Plugin metadata
    name = "bbs"
    keywords = ["bbs", "s", "send"]
    description = (
        "BBS store-and-forward messaging. "
        "'s <name>' to search contacts, 's <N> <msg>' to send, "
        "'bbs r' to read, 'bbs list' for count."
    )
    category = "basic"

    # Documentation
    short_description = "Store-and-forward BBS messages between mesh nodes"
    usage = "s <name> | s <N> <message> | bbs r | bbs list"
    examples = [
        "s Tom",
        "s 2 Hey, are you around?",
        "bbs s Alice",
        "bbs s 1 Meet at noon.",
        "bbs r",
        "bbs list",
    ]
    parameters = [
        {"name": "name", "description": "Partial recipient name to search for"},
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
        # In-memory cooldown: sender_name -> last notification timestamp
        self._notification_cooldowns: dict[str, float] = {}
        # In-memory pending recipient shortlists: sender_key -> (candidates, expires_at)
        self._pending_selections: dict[str, tuple[list[str], float]] = {}

    # ------------------------------------------------------------------
    # BaseCommand interface
    # ------------------------------------------------------------------

    def can_execute(self, message: MeshMessage, skip_channel_check: bool = False) -> bool:
        if not self.enabled:
            return False
        return super().can_execute(message, skip_channel_check)

    def get_help_text(self) -> str:
        return (
            "BBS store-and-forward messaging:\n"
            "  s <name>         — search contacts & show shortlist\n"
            "  s <N> <msg>      — send msg to match N from shortlist\n"
            "  bbs r            — read your pending messages (DM only)\n"
            "  bbs list         — count pending messages (DM only)"
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

            if sub in ("s", "send"):
                return await self._handle_send(message, sub_args)
            if sub in ("r", "read"):
                return await self._handle_read(message)
            if sub in ("l", "list"):
                return await self._handle_list(message)
            if sub == "help":
                return await self._handle_help(message)

            # Unknown subcommand — show help
            return await self._handle_help(message)

        return await self._handle_help(message)

    # ------------------------------------------------------------------
    # Sub-handlers
    # ------------------------------------------------------------------

    async def _handle_send(self, message: MeshMessage, args: str) -> bool:
        """Handle: [bbs] s|send <name_or_number> [message]

        Two-step flow
        -------------
        Step 1 — search:
            s <query>
            Looks up *query* in known contacts.  If multiple matches are found
            a numbered shortlist (max 5) is returned and the selection is kept
            in memory for ``_PENDING_SELECTION_TTL`` seconds.  If there is
            exactly one match *and* a message was already provided, the message
            is sent immediately (no shortlist needed).

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

        sender_key = message.sender_pubkey or message.sender_id or "unknown"
        sender_name = message.sender_id or "unknown"

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

            recipient_name = candidates[idx - 1]
            self._clear_pending_selection(sender_key)

        # ------------------------------------------------------------------
        # Step 1: search for a recipient by partial name
        # ------------------------------------------------------------------
        else:
            matches = self._lookup_contacts(first)
            if not matches:
                await self.send_response(
                    message, f"No contact found matching '{first}'."
                )
                return True

            if len(matches) == 1 and rest:
                # Single unambiguous match with message already present — send directly.
                recipient_name = matches[0]
            else:
                # Multiple matches, or single match without a message → show shortlist.
                self._set_pending_selection(sender_key, matches)
                lines = "\n".join(f"{i + 1} {name}" for i, name in enumerate(matches))
                await self.send_response(message, f"{lines}\nReply: s <N> <message>")
                return True

        # Store the confirmed message
        self._purge_old_messages()
        success = self._store_message(sender_key, sender_name, recipient_name, rest)
        if success:
            await self.send_response(message, f"Message stored for {recipient_name}.")
        else:
            await self.send_response(
                message,
                f"Could not store message for {recipient_name} (mailbox full).",
            )
        return True

    async def _handle_read(self, message: MeshMessage) -> bool:
        """Handle: bbs r|read — read pending messages (DM only for privacy)."""
        if not message.is_dm:
            await self.send_response(message, "BBS read is only available via DM.")
            return True

        sender_name = message.sender_id
        if not sender_name:
            await self.send_response(message, "Unable to identify sender.")
            return True

        messages = self._get_pending_messages(sender_name)
        if not messages:
            await self.send_response(message, "No pending BBS messages.")
            return True

        # Mark all retrieved messages as read
        self._mark_messages_read(sender_name)

        # Build per-message strings
        lines: list[str] = []
        for row in messages:
            _msg_id, from_name, text, sent_at = row
            try:
                dt = datetime.fromisoformat(str(sent_at))
                date_str = dt.strftime("%m/%d %H:%M")
            except Exception:
                date_str = str(sent_at)[:10]
            lines.append(f"From {from_name} [{date_str}]: {text}")

        # Send chunked if responses won't fit in one message
        full_response = "\n".join(lines)
        if len(full_response.encode("utf-8")) > _MAX_SINGLE_MESSAGE_BYTES:
            for line in lines:
                await self.send_response(message, line, skip_user_rate_limit=True)
        else:
            await self.send_response(message, full_response)
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

        count = self._get_pending_count(sender_name)
        if count == 0:
            await self.send_response(message, "No pending BBS messages.")
        else:
            await self.send_response(
                message,
                f"You have {count} pending BBS message(s). Send 'bbs r' to read.",
            )
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
        Skips notification if the user is already running a BBS read/list
        command, or if we notified them recently.

        Args:
            message: The inbound DM message.
        """
        if not self.enabled or not message.is_dm:
            return

        # Don't double-notify when the user is already reading/listing
        content_lower = message.content.strip().lower()
        if self._is_bbs_read_command(content_lower):
            return

        sender_name = message.sender_id
        if not sender_name:
            return

        # Enforce per-user notification cooldown
        now = time.time()
        last_notified = self._notification_cooldowns.get(sender_name, 0.0)
        if now - last_notified < NOTIFICATION_COOLDOWN_SECONDS:
            return

        count = self._get_pending_count(sender_name)
        if count > 0:
            self._notification_cooldowns[sender_name] = now
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
    def _is_bbs_read_command(content_lower: str) -> bool:
        """Return True if the message is a BBS read or list command."""
        read_cmds = {"bbs r", "bbs read", "bbs l", "bbs list"}
        return content_lower in read_cmds or content_lower.startswith("bbs r ") or content_lower.startswith("bbs l ")

    # ------------------------------------------------------------------
    # Contact lookup
    # ------------------------------------------------------------------

    def _lookup_contacts(self, query: str) -> list[str]:
        """Return up to 5 known contact names that contain *query* (case-insensitive).

        Queries the ``complete_contact_tracking`` table.  Returns an empty list
        when that table does not yet exist (e.g. fresh install before migrations
        have run) or when no contacts match.
        """
        try:
            with self.bot.db_manager.connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type='table' AND name='complete_contact_tracking'"
                )
                if not cursor.fetchone():
                    return []
                cursor.execute(
                    "SELECT DISTINCT name FROM complete_contact_tracking "
                    "WHERE name LIKE ? COLLATE NOCASE "
                    "ORDER BY last_heard DESC LIMIT 5",
                    (f"%{query}%",),
                )
                return [row[0] for row in cursor.fetchall()]
        except Exception as e:
            self.logger.error(f"Error looking up BBS contacts: {e}")
            return []

    # ------------------------------------------------------------------
    # Pending recipient-selection state
    # ------------------------------------------------------------------

    def _get_pending_selection(self, sender_key: str) -> list[str] | None:
        """Return the unexpired candidate list for *sender_key*, or ``None``."""
        entry = self._pending_selections.get(sender_key)
        if entry is None:
            return None
        candidates, expires_at = entry
        if time.time() > expires_at:
            self._pending_selections.pop(sender_key, None)
            return None
        return candidates

    def _set_pending_selection(self, sender_key: str, candidates: list[str]) -> None:
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

    def _store_message(
        self,
        sender_id: str,
        sender_name: str,
        recipient_name: str,
        message_text: str,
    ) -> bool:
        """Store a BBS message for *recipient_name*.

        Returns True on success, False if the mailbox is full or an error occurs.
        """
        try:
            with self.bot.db_manager.connection() as conn:
                cursor = conn.cursor()
                recipient_lower = recipient_name.lower()
                cursor.execute(
                    "SELECT COUNT(*) FROM bbs_messages "
                    "WHERE recipient_name = ? AND read_at IS NULL",
                    (recipient_lower,),
                )
                count = cursor.fetchone()[0]
                if count >= self.max_messages_per_user:
                    self.logger.warning(
                        f"BBS mailbox full for {recipient_name} ({count} unread)"
                    )
                    return False

                conn.execute(
                    "INSERT INTO bbs_messages "
                    "(sender_id, sender_name, recipient_name, message) "
                    "VALUES (?, ?, ?, ?)",
                    (sender_id, sender_name, recipient_lower, message_text),
                )
                conn.commit()
                self.logger.info(
                    f"BBS: {sender_name} -> {recipient_name}: {message_text[:50]}"
                )
                return True
        except Exception as e:
            self.logger.error(f"Error storing BBS message: {e}")
            return False

    def _get_pending_count(self, recipient_name: str) -> int:
        """Return the number of unread messages waiting for *recipient_name*."""
        try:
            with self.bot.db_manager.connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT COUNT(*) FROM bbs_messages "
                    "WHERE recipient_name = ? COLLATE NOCASE AND read_at IS NULL",
                    (recipient_name,),
                )
                return cursor.fetchone()[0]
        except Exception as e:
            self.logger.error(f"Error counting BBS messages: {e}")
            return 0

    def _get_pending_messages(self, recipient_name: str) -> list:
        """Return all unread messages for *recipient_name*, oldest first."""
        try:
            with self.bot.db_manager.connection() as conn:
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
            self.logger.error(f"Error fetching BBS messages: {e}")
            return []

    def _mark_messages_read(self, recipient_name: str) -> int:
        """Mark all unread messages for *recipient_name* as read.

        Returns the number of rows updated.
        """
        try:
            with self.bot.db_manager.connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "UPDATE bbs_messages "
                    "SET read_at = CURRENT_TIMESTAMP "
                    "WHERE recipient_name = ? COLLATE NOCASE AND read_at IS NULL",
                    (recipient_name,),
                )
                count = cursor.rowcount
                conn.commit()
                return count
        except Exception as e:
            self.logger.error(f"Error marking BBS messages as read: {e}")
            return 0

    def _purge_old_messages(self) -> None:
        """Delete messages older than *max_message_age_days*."""
        try:
            with self.bot.db_manager.connection() as conn:
                conn.execute(
                    "DELETE FROM bbs_messages "
                    "WHERE sent_at < datetime('now', ? || ' days')",
                    (f"-{self.max_message_age_days}",),
                )
                conn.commit()
        except Exception as e:
            self.logger.debug(f"Error purging old BBS messages: {e}")
