#!/usr/bin/env python3
"""
BBS (Bulletin Board System) command for the MeshCore Bot.

Provides per-user store-and-forward messaging so nodes can leave messages
for other nodes to read when they next contact the bot.

Usage examples:
  s <name> <message>           — send a message (shorthand)
  send <name> <message>        — send a message
  bbs s <name> <message>       — send a message
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
        "'s <name> <msg>' to send, 'bbs r' to read, 'bbs list' for count."
    )
    category = "basic"

    # Documentation
    short_description = "Store-and-forward BBS messages between mesh nodes"
    usage = "s <name> <message> | bbs r | bbs list"
    examples = [
        "s John Hey, are you around?",
        "send Alice Meet at noon.",
        "bbs s Bob Roger that!",
        "bbs r",
        "bbs list",
    ]
    parameters = [
        {"name": "name", "description": "Recipient's node name"},
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
            "  s <name> <msg>   — store a message for a user\n"
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
        """Handle: [bbs] s|send <name> <message>"""
        if not args:
            await self.send_response(message, "Usage: s <name> <message>")
            return True

        parts = args.split(None, 1)
        if len(parts) < 2:
            await self.send_response(message, "Usage: s <name> <message>")
            return True

        recipient_name, msg_text = parts[0], parts[1]

        sender_name = message.sender_id or "unknown"
        sender_id = message.sender_pubkey or message.sender_id or "unknown"

        self._purge_old_messages()

        success = self._store_message(sender_id, sender_name, recipient_name, msg_text)
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
