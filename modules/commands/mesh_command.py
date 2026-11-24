"""Public entry to the mesh database capability, formerly inside ask."""
from ..assistant.mesh_service import MeshService
from ..assistant.response import send_answer
from ..assistant.sender_policy import sender_may_use_llm
from ..models import MeshMessage
from .base_command import BaseCommand


class MeshCommand(BaseCommand):
    name = "mesh"
    keywords = ["mesh", "query", "sql"]
    description = "Ask about locally observed mesh network data"
    short_description = description
    category = "analytics"
    cooldown_seconds = 30
    usage = "mesh <question>"
    examples = ["mesh combien de répéteurs actifs sur 7 jours"]
    settings_schema = [
        {"key": "public_enabled", "label": "Direct mesh command", "type": "bool", "default": True,
         "help": "Disable the public command while keeping the capability available to ask."},
    ]

    def __init__(self, bot):
        super().__init__(bot)
        self.enabled = self.get_config_value("Mesh_Command", "enabled", fallback=True, value_type="bool")
        self.public_enabled = self.get_config_value("Mesh_Command", "public_enabled", fallback=True, value_type="bool")
        self.service = MeshService(bot, self.get_config_value)

    def can_use_service(self, message: MeshMessage, skip_channel_check: bool = False) -> bool:
        return sender_may_use_llm(self.bot, message) and self.enabled and super().can_use_service(message, skip_channel_check)

    def can_execute(self, message: MeshMessage, skip_channel_check: bool = False) -> bool:
        return self.public_enabled and self.can_use_service(message, skip_channel_check)

    async def execute(self, message: MeshMessage) -> bool:
        if not sender_may_use_llm(self.bot, message):
            return False
        _, question = self.split_trigger_and_args(message.content)
        if not question or question.lower() in {"help", "?", "h"}:
            return await self.send_response(message, f"Usage: {self._command_prefix}mesh <question sur le réseau>")
        if question.lower() in {"tables", "schema", "db"}:
            return await send_answer(self, message, await self.service.answer(question, message), max_pages=8)
        return await send_answer(self, message, await self.service.answer(question, message))
