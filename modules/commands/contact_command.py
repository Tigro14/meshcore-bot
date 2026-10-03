#!/usr/bin/env python3
"""
Contact command for the MeshCore Bot
Adds the bot contact info to the current channel
"""

import re
import urllib.parse
from typing import Any, Optional

from modules.commands.base_command import BaseCommand
from modules.models import MeshMessage

PUBLIC_KEY_RE = re.compile(r'^[0-9a-fA-F]{64}$')

# Maps AdvType (from self_info["adv_type"]) to MeshCore contact QR "type" param
# AdvType: NONE=0, CHAT=1, REPEATER=2, ROOM=3, SENSOR=4
# QR type: 1=Companion, 2=Repeater, 3=Room Server, 4=Sensor
ADV_TYPE_TO_CONTACT_TYPE = {
    1: 1,  # CHAT   -> Companion
    2: 2,  # REPEATER -> Repeater
    3: 3,  # ROOM   -> Room Server
    4: 4,  # SENSOR -> Sensor
}
DEFAULT_CONTACT_TYPE = 1  # Companion


class ContactCommand(BaseCommand):
    """Handles contact command"""

    # Plugin metadata
    name = "contact"
    keywords = ['contact']
    description = "Display the bot's contact information"
    category = "basic"

    # Documentation
    short_description = "Display the bot's contact information"
    usage = "contact"
    examples = [
        "contact"
    ]

    def __init__(self, bot):
        """Initialize the contact command.

        Args:
            bot: The bot instance.
        """
        super().__init__(bot)
        self.enabled = self.get_config_value('Contact_Command', 'enabled', fallback=True, value_type='bool')

    def can_execute(self, message: MeshMessage, skip_channel_check: bool = False) -> bool:
        """Check if this command can be executed with the given message.

        Args:
            message: The message triggering the command.
            skip_channel_check: If True, skip the channel check.

        Returns:
            bool: True if command is enabled and checks pass, False otherwise.
        """
        if not self.enabled:
            return False
        return super().can_execute(message, skip_channel_check=skip_channel_check)

    def get_help_text(self) -> str:
        """Get help text for the contact command.

        Returns:
            str: Help text string.
        """
        return self.translate('commands.contact.help')

    def matches_keyword(self, message: MeshMessage) -> bool:
        """Match ``contact`` (or a configured alias) on its own, with no arguments.

        Args:
            message: The received message.

        Returns:
            bool: True if the message is a contact command, False otherwise.
        """
        def _matches(content_lower: str) -> bool:
            return any(content_lower == keyword.lower() for keyword in self.keywords)

        return self._cleaned_content_matches(message, _matches)

    def _self_info_raw(self, key: str):
        """Read a raw field from the radio's self_info, which may be a dict or an object.

        Args:
            key: The self_info field name.

        Returns:
            The field value, or None if unavailable.
        """
        meshcore: Any = getattr(self.bot, 'meshcore', None)
        self_info = getattr(meshcore, 'self_info', None) if meshcore else None
        if not self_info:
            return None
        if isinstance(self_info, dict):
            return self_info.get(key)
        return getattr(self_info, key, None)

    def _self_info_value(self, key: str) -> Optional[str]:
        """Read a string field from the radio's self_info.

        Args:
            key: The self_info field name.

        Returns:
            str: The field value, or None if unavailable.
        """
        value = self._self_info_raw(key)
        return str(value).strip() if value else None

    def _contact_type(self) -> int:
        """Determine the MeshCore contact type from the radio's adv_type.

        Returns:
            int: The contact type (1=Companion, 2=Repeater, 3=Room Server, 4=Sensor).
        """
        adv_type = self._self_info_raw('adv_type')
        if adv_type is not None:
            try:
                return ADV_TYPE_TO_CONTACT_TYPE.get(int(adv_type), DEFAULT_CONTACT_TYPE)
            except (ValueError, TypeError):
                pass
        return DEFAULT_CONTACT_TYPE

    async def execute(self, message: MeshMessage) -> bool:
        """Execute the contact command.

        Args:
            message: The message triggering the command.

        Returns:
            bool: True if executed successfully, False otherwise.
        """
        public_key = self._self_info_value('public_key')
        name = self._self_info_value('name') or self._self_info_value('adv_name')

        if not public_key or not PUBLIC_KEY_RE.match(public_key):
            self.logger.warning("Contact command: no usable public key in self_info")
            return await self.send_response(message, self.translate('commands.contact.unavailable'))

        if not name:
            self.logger.warning("Contact command: no device name in self_info")
            return await self.send_response(message, self.translate('commands.contact.unavailable'))

        encoded_name = urllib.parse.quote(name, safe='')
        contact_type = self._contact_type()
        return await self.send_response(
            message,
            f"meshcore://contact/add?name={encoded_name}&public_key={public_key.lower()}&type={contact_type}"
        )
