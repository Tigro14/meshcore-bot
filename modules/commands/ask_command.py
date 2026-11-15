"""Assistant entry point. Routing and capabilities live in modules.assistant."""
import re

from ..assistant.dispatcher import AssistantDispatcher
from ..assistant.response import send_answer
from ..assistant.sender_policy import sender_may_use_llm
from ..models import MeshMessage
from .base_command import BaseCommand


class AskCommand(BaseCommand):
    name = "ask"
    keywords = ["ask"]
    description = "Assistant: route questions to mesh data, Wiki, RF tools or conversation"
    short_description = description
    category = "special"
    cooldown_seconds = 5
    usage = "ask <question>"
    examples = ["ask comment tu me reçois ?", "ask combien de répéteurs actifs ?"]
    settings_schema = [
        {"key": "enabled_routes", "label": "Enabled routes", "type": "list",
         "default": "test,path,mesh,wiki,weather,llm,bbs,info", "help": "Allowed capabilities: test,path,mesh,wiki,weather,llm,bbs,info."},
        {"key": "route_timeout_seconds", "label": "Processing timeout", "type": "int",
         "default": 120, "min": 1, "max": 300, "unit": "seconds"},
        {"key": "semantic_timeout_seconds", "label": "Classification timeout", "type": "int",
         "default": 300, "min": 1, "max": 300, "unit": "seconds"},
        {"key": "semantic_routing_enabled", "label": "Closed-catalog LLM routing", "type": "bool",
         "default": True, "help": "Select a validated operation from enabled_routes. Failure returns unavailable; no Wiki fallback."},
        {"key": "max_pages", "label": "Maximum reply pages", "type": "int",
         "default": 1, "min": 1, "max": 8},
    ]

    def __init__(self, bot):
        super().__init__(bot)
        self.ask_enabled = self.get_config_value("Ask_Command", "enabled", fallback=True, value_type="bool")
        self.enabled_routes = {r.strip().lower() for r in self.get_config_value(
            "Ask_Command", "enabled_routes", fallback="test,path,mesh,wiki,weather,llm,bbs,info", value_type="str"
        ).split(",") if r.strip()}
        unknown = self.enabled_routes - {"test", "path", "mesh", "wiki", "weather", "llm", "bbs", "info"}
        if unknown:
            raise ValueError(f"Unknown assistant routes: {sorted(unknown)}")
        self.route_timeout_seconds = max(1, min(300, self.get_config_value(
            "Ask_Command", "route_timeout_seconds", fallback=120, value_type="int")))
        self.max_pages = max(1, min(8, self.get_config_value("Ask_Command", "max_pages", fallback=1, value_type="int")))
        self.dispatcher = AssistantDispatcher(self)

    def can_execute(self, message: MeshMessage, skip_channel_check: bool = False) -> bool:
        return sender_may_use_llm(self.bot, message) and self.ask_enabled and super().can_execute(message, skip_channel_check)

    def _punctuated_trigger(self, content: str) -> tuple[str, str] | None:
        """Recognize punctuation directly after an assistant keyword or alias."""
        for keyword in sorted(self.keywords, key=lambda k: (len(k.split()), len(k)), reverse=True):
            words = keyword.split()
            if not words:
                continue
            pattern = r'\s+'.join(re.escape(word) for word in words) + r'[,;:!?]+\s*(.*)'
            match = re.fullmatch(pattern, content.strip(), flags=re.I | re.S)
            if match:
                return keyword.lower(), match.group(1).strip()
        return None

    def split_trigger_and_args(self, content: str) -> tuple[str | None, str]:
        trigger, question = super().split_trigger_and_args(content)
        if trigger is not None:
            return trigger, re.sub(r'^[,;:!?]+\s*', '', question)
        return self._punctuated_trigger(question) or (None, question)

    def mentioned_question(self, message: MeshMessage, content: str) -> str | None:
        # The channel handler may already have removed the mention. Match the
        # immutable on-air text, including bot names containing brackets.
        mode = self.bot.config.get('Bot', 'respond_to_mentions', fallback='also').strip().lower()
        name = self._get_bot_name()
        mention = f'@[{name}]' if name else ''
        original = message.original_content or message.content
        if mode != 'false' and mention and mention.casefold() in original.casefold():
            body = re.sub(re.escape(mention), '', content, flags=re.IGNORECASE).strip()
            _, question = self.split_trigger_and_args(body)
            return question
        return None

    def matches_keyword(self, message: MeshMessage) -> bool:
        question = self.mentioned_question(message, message.content)
        if question is not None:
            message.content = 'ask ' + question
            message.content_lower = message.content.lower()
            return True
        if super().matches_keyword(message):
            return True
        return self._cleaned_content_matches(
            message, lambda content: self._punctuated_trigger(content) is not None
        )

    def bbs_text_pending(self, message: MeshMessage, content: str | None = None) -> bool:
        """Claim only the explicitly requested body in this sender's BBS session."""
        if not getattr(self, 'ask_enabled', False) or 'bbs' not in getattr(self, 'enabled_routes', ()):
            return False
        service = getattr(getattr(self, 'dispatcher', None), 'bbs', None)
        context = service.context(message) if service is not None else None
        if not context or context['awaiting'] != 'text':
            return False
        if message.is_dm:
            return True
        # A BBS session never makes the bot listen to unaddressed channel text.
        body = message.content if content is None else content
        trigger, _ = self.split_trigger_and_args(body)
        return trigger is not None or self.mentioned_question(message, body) is not None

    def claim_bbs_text(self, message: MeshMessage, content: str | None = None) -> bool:
        if message.bbs_body_session is not None:
            return True
        if not self.bbs_text_pending(message, content):
            return False
        message.bbs_body_session = self.dispatcher.bbs.body_session(message)
        return message.bbs_body_session is not None

    async def execute(self, message: MeshMessage) -> bool:
        if not sender_may_use_llm(self.bot, message):
            return False
        # Matching may strip mentions or collapse spaces. Read the immutable
        # on-air body so a BBS message retains mentions and original spacing.
        source = message.original_content or message.content
        name = self._get_bot_name()
        if isinstance(name, str) and name:
            source = re.sub(r'^\s*' + re.escape('@[' + name + ']') + r'\s*', '', source, count=1, flags=re.I)
            trigger, _ = self.split_trigger_and_args(source)
            if not message.is_dm and trigger is None:
                source = re.sub(r'\s*' + re.escape('@[' + name + ']') + r'\s*$', '', source, count=1, flags=re.I)
        if message.is_dm and self.bbs_text_pending(message):
            question = message.original_content or message.content
        else:
            _, question = self.split_trigger_and_args(source)
        text = await self.dispatcher.answer(question, message)
        if text is None:  # BBS has already sent the reply and committed delivery.
            return True
        return await send_answer(self, message, text, max_pages=self.max_pages)
