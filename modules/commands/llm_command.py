"""Direct LLM command; shared engine lives outside command routing."""
from ..assistant.llm_service import LlmService
from ..assistant.response import send_answer
from ..assistant.sender_policy import sender_may_use_llm
from ..models import MeshMessage
from .base_command import BaseCommand


class LlmCommand(BaseCommand):
    name = "llm"
    keywords = ["llm", "ia", "ai", "chat"]
    description = "Direct conversation with the configured LLM"
    category = "basic"
    cooldown_seconds = 5
    short_description = description
    usage = "llm <question>"
    examples = ["llm What is APRS?"]
    settings_schema = [
        {"key": "public_enabled", "label": "Direct llm command", "type": "bool", "default": True,
         "help": "Disable direct llm access while keeping the service available to ask."},
    ]

    def __init__(self, bot):
        super().__init__(bot)
        self.llm_enabled = self.get_config_value("Llm_Command", "enabled", fallback=False, value_type="bool")
        self.public_enabled = self.get_config_value("Llm_Command", "public_enabled", fallback=True, value_type="bool")
        self.service = LlmService(bot, self.get_config_value)

    def can_execute(self, message: MeshMessage, skip_channel_check: bool = False) -> bool:
        return self.public_enabled and self.can_use_service(message, skip_channel_check)

    def can_use_service(self, message: MeshMessage, skip_channel_check: bool = False) -> bool:
        return sender_may_use_llm(self.bot, message) and self.llm_enabled and super().can_use_service(message, skip_channel_check)

    def get_help_text(self, message: MeshMessage | None = None) -> str:
        return f"Usage: {self._command_prefix}llm <question>"

    def _extract_prompt(self, message: MeshMessage) -> str:
        _, prompt = self.split_trigger_and_args(self._strip_mentions(message.content))
        return prompt if _ else ""

    async def execute(self, message: MeshMessage) -> bool:
        if not sender_may_use_llm(self.bot, message):
            return False
        prompt = self._extract_prompt(message)
        if not prompt:
            return await self.send_response(message, self.get_help_text())
        text = await self.service.answer(prompt, message, mode="auto", max_length=self.get_max_message_length(message))
        return await send_answer(self, message, text, max_pages=self.service.page_count if self.service.pagination_enabled else 1)
