"""Capability invocation, retaining caller identity and command restrictions."""
import asyncio
import re
import unicodedata
from copy import deepcopy
from typing import Any

from ..models import MeshMessage
from .response import split_reply
from .router import Decision, Route
from .semantic_router import SemanticRouter
from .sender_policy import sender_may_use_llm

HELP = "ask <question> — réseau, Wiki, météo, réception ou chemin. Boîte aux lettres : laisse un message, lis mes messages."


class AssistantDispatcher:
    def __init__(self, owner: Any):
        self.owner = owner
        self.semantic_router = SemanticRouter(owner)
        self._rf_lock = asyncio.Lock()
        from .bbs_service import BBSAssistant
        self.bbs = BBSAssistant(self)

    def record_function(self, function: str) -> None:
        from .usage import record
        record(self.owner.bot, function)

    def _command(self, name: str) -> Any:
        return self.owner.bot.command_manager.commands.get(name)

    def _allowed(self, command: Any, message: MeshMessage, *, service: bool = False) -> bool:
        if command is None:
            return False
        check = command.can_use_service if service else command.can_execute
        return bool(check(message))

    async def answer(self, question: str, message: MeshMessage) -> str | None:
        if not sender_may_use_llm(self.owner.bot, message):
            return ""
        if not question.strip():
            return HELP
        context = self.bbs.context(message) if hasattr(self, "bbs") else None
        body_session = getattr(message, 'bbs_body_session', None)
        if body_session is not None and self.bbs.body_session(message) != body_session:
            return 'Cet envoi BBS a expiré ou a changé. Précise à nouveau le destinataire et le message.'
        if context and context['awaiting'] == 'text':
            # The user is filling an explicitly requested message body. It is
            # literal data, not a new tool request, and never goes to the LLM.
            from .router import normalize
            cancel = normalize(question) in {"annule", "annule l'envoi", "bbs cancel"}
            decision = Decision(Route.BBS, question, 'bbs_body',
                                'cancel' if cancel else 'continue',
                                {} if cancel else {'selection': '', 'text': question})
        elif context:
            decision = await self.semantic_router.decide(question, self.owner.enabled_routes, bbs_context=context)
        else:
            decision = await self.semantic_router.decide(question, self.owner.enabled_routes)
        if decision is None:
            return "Le service d’interprétation est momentanément indisponible. Réessaie plus tard."
        self.owner.logger.info("Assistant route=%s operation=%s reason=%s", decision.route.value, decision.operation, decision.reason)
        if decision.route is Route.HELP:
            return HELP
        if not decision.question and decision.route not in {Route.TEST, Route.PATH}:
            return HELP
        try:
            return await asyncio.wait_for(self._dispatch(decision, message), self.owner.route_timeout_seconds)
        except TimeoutError:
            return "Le traitement a dépassé le délai. Réessaie plus tard."
        except Exception:
            self.owner.logger.exception("Assistant capability failed: %s", decision.route.value)
            return "Ce traitement est momentanément indisponible."

    async def _dispatch(self, decision: Decision, message: MeshMessage) -> str | None:
        route = decision.route
        if route.value not in self.owner.enabled_routes:
            return f"La fonction {route.value} est désactivée."
        if route is Route.INFO:
            from .provenance import answer_source
            return answer_source(self, decision.args['topic'], message)
        if route is Route.BBS:
            return await self.bbs.execute(decision, message)
        if route is Route.PATH and decision.operation == 'hops':
            command = self._command('path')
            if not self._allowed(command, message, service=True):
                return 'Le nombre de sauts est indisponible ou non autorisé ici.'
            from .message_hops import hops_reply
            self.record_function('path.hops')
            command.record_execution(message.sender_id or None)
            return hops_reply(message)
        if route in {Route.TEST, Route.PATH}:
            return await self._rf_tool(route.value, decision.question, message)
        if route is Route.WEATHER:
            if decision.operation == 'provider':
                from .provenance import answer_source
                return answer_source(self, 'weather', message, function='weather.provider')
            return await self._weather_tool(decision.question, message, decision.args)
        if route is Route.MESH:
            from .network_tools import answer_network
            return await answer_network(self, decision, message)
        command = self._command("llm")
        if not self._allowed(command, message, service=True):
            return f"La fonction {route.value} est indisponible ou non autorisée ici."
        mode = "wiki"
        if route is Route.LLM:
            mode = "clarify" if decision.operation == "clarify" else "general"
        self.record_function(
            "wiki.lookup" if mode == "wiki" else
            "llm.clarify" if mode == "clarify" else "llm.chat"
        )
        command.record_execution(message.sender_id or None)
        options = {}
        mention = ""
        if mode in {"general", "clarify"}:
            budget = self.owner.get_max_message_length(message)
            if mode == 'clarify':
                from .response import clarification_mention
                mention = clarification_mention(message, budget)
            options["max_length"] = budget - len(mention.encode('utf-8'))
        answer = await command.service.answer(
            decision.question, message, mode=mode, **options
        )
        return mention + answer

    async def _render_tool(self, question, source, message, context=""):
        budget = self.owner.get_max_message_length(message)
        llm = self._command("llm")
        if self._allowed(llm, message, service=True):
            result = await llm.service.rephrase_tool_result(
                question, source, message=message,
                context=(context or "Faits mesurés localement.") + f" Ne change aucun nom ni mesure. Un message, {budget} octets UTF-8 maximum.",
                max_length=budget,
            )
            if result and len(result.encode("utf-8")) <= budget:
                return result
        return split_reply(source, budget, max_pages=1)[0]

    async def _rf_tool(self, name: str, question: str, message: MeshMessage) -> str:
        # Existing RF commands have no pure service yet. An explicit adapter uses
        # the established task-local capture contract, never monkeypatches send.
        from ..commands.path_command import PathCommand
        from ..commands.test_command import TestCommand
        command = self._command(name)
        expected = {"test": TestCommand, "path": PathCommand}[name]
        if type(command) is not expected:
            return f"L'outil {name} n'est pas disponible pour le routage."
        async with self._rf_lock:
            cloned = deepcopy(message)
            cloned.content = name
            cloned.content_lower = name
            cloned.prefix_normalized = True
            cloned.capture_sink = []
            if not self._allowed(command, cloned, service=True):
                return f"L'outil {name} est désactivé, limité ou non autorisé ici."
            self.record_function("test.receive" if name == "test" else "path.message")
            command.record_execution(message.sender_id or None)
            await command.execute(cloned)
            if cloned.capture_sink:
                raw_answer = "\n".join(cloned.capture_sink)
                if name == "test":
                    llm = self._command("llm")
                    if self._allowed(llm, message, service=True):
                        measurements = self._reception_measurements(message, raw_answer)
                        reformulated = await llm.service.rephrase_tool_result(
                            question,
                            measurements,
                            message=message,
                            context=(
                                "Il s'agit de la mesure de réception du message par le bot. "
                                "Réponds directement par oui et reformule brièvement les mesures. "
                                "Ne qualifie pas la liaison (bonne, moyenne, faible ou médiocre) "
                                "si cette qualification n'est pas déjà présente dans les données."
                            ),
                            max_length=220,
                        )
                        if reformulated:
                            return reformulated
                return await self._render_tool(question, raw_answer, message)
            if name == "path":
                return "Ce message ne contient pas de chemin radio exploitable."
            return "Ce message ne contient pas de mesure radio exploitable."

    @staticmethod
    def _reception_measurements(message: MeshMessage, fallback: str) -> str:
        """Build a format-independent summary of the received RF measurements."""
        values = ["Message reçu par le bot"]
        if message.snr is not None:
            values.append(f"SNR : {message.snr} dB")
        if message.rssi is not None:
            values.append(f"RSSI : {message.rssi} dBm")
        if message.hops is not None:
            if message.hops > 0:
                values.append(f"Nombre de sauts : {message.hops}")
            else:
                values.append("Réception directe")
        if message.path:
            values.append(f"Chemin radio : {message.path}")
        return " ; ".join(values) if len(values) > 1 else fallback

    @staticmethod
    def _weather_command(question: str, default_location: str = "") -> str:
        """Turn a natural-language forecast question into the existing wx syntax."""
        folded = "".join(
            char for char in unicodedata.normalize("NFKD", question.casefold())
            if not unicodedata.combining(char)
        ).replace("’", "'").replace("`", "'")
        option = "tomorrow" if re.search(r"\b(demain|tomorrow)\b", folded) else ""
        # Prefer a location introduced by an unambiguous preposition. This
        # covers French and English while leaving a bare request to wx's normal
        # companion/default-location handling.
        location = ""
        matches = list(re.finditer(r"\b(?:a|pour|in|for)\s+([^?!.]+)", folded))
        if matches:
            location = matches[-1].group(1).strip()
        location = re.sub(
            r"\b(?:demain|tomorrow|aujourd'hui|today|ce soir|tonight)\b",
            "",
            location,
        ).strip(" ,")
        if not location:
            location = default_location.strip()
        return " ".join(part for part in ("wx", location, option) if part)

    @staticmethod
    def _expand_weather_notation(source: str, wind_unit: str = "") -> str:
        """Give the LLM labelled values instead of ambiguous compact WX codes."""
        # Compact providers append point-dew, visibility and pressure fields
        # behind pictograms. They make the one-packet assistant answer noisy;
        # the direct wx command remains available when those details are wanted.
        expanded = re.sub(
            r"💧\ufe0f?\s*-?\d+(?:[.,]\d+)?\s*°[CF]?",
            "",
            source,
            flags=re.IGNORECASE,
        )
        expanded = re.sub(
            r"👁\ufe0f?\s*-?\d+(?:[.,]\d+)?\s*(?:km|mi)",
            "",
            expanded,
            flags=re.IGNORECASE,
        )
        expanded = re.sub(
            r"(?:📊|📈)\ufe0f?\s*-?\d+(?:[.,]\d+)?\s*(?:hpa|mmhg|inhg)",
            "",
            expanded,
            flags=re.IGNORECASE,
        )
        # Preserve the meaning carried by the rain icon before stripping it.
        expanded = re.sub(r"🌦\ufe0f?\s*(\d+(?:[.,]\d+)?)\s*%", r"probabilité de pluie : \1 %", expanded)
        # Remove decorative weather symbols before handing the text to the LLM.
        expanded = re.sub(r"[\U0001F000-\U0001FAFF\u2600-\u26FF\u2700-\u27BF\u2190-\u21FF]\ufe0f?", "", expanded)
        expanded = re.sub(
            r"\bH\s*:\s*(-?\d+(?:[.,]\d+)?\s*°[CF]?)",
            r"température maximale : \1",
            expanded,
            flags=re.IGNORECASE,
        )
        expanded = re.sub(
            r"\bL\s*:\s*(-?\d+(?:[.,]\d+)?\s*°[CF]?)",
            r"température minimale : \1",
            expanded,
            flags=re.IGNORECASE,
        )
        # Open-Meteo puts the current temperature directly after the weather
        # condition, before the labelled H/L extrema. Label it explicitly so
        # the rephrasing model cannot present it as another maximum.
        current_match = re.search(
            r"(\b(?:aujourd['’]hui|today|demain|tomorrow)\s*:\s*[^|;\n]*?)"
            r"(-?\d+(?:[.,]\d+)?\s*°[CF]?)",
            expanded,
            flags=re.IGNORECASE,
        )
        if current_match and not re.search(
            r"température\s+(?:maximale|minimale)",
            current_match.group(1),
            flags=re.IGNORECASE,
        ):
            expanded = (
                expanded[:current_match.start(2)]
                + "température actuelle : "
                + current_match.group(2)
                + expanded[current_match.end(2):]
            )
        unit = f" {wind_unit}" if wind_unit else ""
        expanded = re.sub(
            r"(?<![\w.,])([NSEOW]{1,3})?(-?\d+(?:[.,]\d+)?)G(-?\d+(?:[.,]\d+)?)(?![\w.,])",
            lambda match: (
                f"vent : {(match.group(1) + ' ') if match.group(1) else ''}"
                f"{match.group(2)}{unit}; rafales : {match.group(3)}{unit}"
            ),
            expanded,
            flags=re.IGNORECASE,
        )
        expanded = re.sub(
            r"(-?\d+(?:[.,]\d+)?)\s*%RH\b",
            r"humidité : \1 %",
            expanded,
            flags=re.IGNORECASE,
        )
        return re.sub(r"\s{2,}", " ", expanded).strip()

    @staticmethod
    def _select_weather_period(question: str, source: str) -> str:
        """Keep the forecast period requested instead of cramming in the next day."""
        folded = "".join(
            char for char in unicodedata.normalize("NFKD", question.casefold())
            if not unicodedata.combining(char)
        )
        if re.search(r"\b(demain|tomorrow)\b", folded):
            return source
        # The default Open-Meteo reply appends tomorrow after today's values.
        # ASK answers only the requested current/today period in its one packet.
        return re.split(
            r"\s*\|\s*(?:demain|tomorrow)\s*:",
            source,
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0].strip()

    @staticmethod
    def _replace_weather_location(source: str, location: str) -> str:
        """Keep the configured regional label instead of a geocoder centroid town."""
        if not location.strip():
            return source
        return re.sub(
            r"^[^:\n]+:",
            f"{location.strip()}:",
            source,
            count=1,
        )

    @staticmethod
    def _enforce_weather_location(answer: str, display_location: str) -> str:
        """Keep the configured display label after non-deterministic LLM rephrasing."""
        label = display_location.strip()
        if not label:
            return answer
        head, separator, tail = answer.partition(":")
        if not separator:
            return f"{label} : {answer.strip()}"
        period = re.search(
            r"\b(aujourd['’]hui|demain|today|tomorrow)\b",
            head,
            flags=re.IGNORECASE,
        )
        if period and period.group(1).casefold() not in label.casefold():
            label = f"{label} {period.group(1)}"
        return f"{label} :{tail}"

    @staticmethod
    def _weather_source_location(source: str) -> str:
        """Read the provider's location heading; never infer a place from prose."""
        heading, separator, _ = source.partition(":")
        if not separator:
            return ""
        location = heading.split(",", 1)[0].strip()
        period = r"aujourd['’]hui|demain|today|tomorrow"
        if re.fullmatch(period, location, flags=re.IGNORECASE):
            return ""
        return re.sub(r"\s+(?:" + period + r")$", "", location, flags=re.IGNORECASE).strip()

    @staticmethod
    def _weather_fallback(source: str, budget: int) -> str:
        """Fit complete factual clauses instead of slicing expanded labels."""
        text = re.sub(r"température actuelle\s*:\s*", "", source, flags=re.I)
        text = re.sub(r"température maximale\s*:\s*", "T° Max ", text, flags=re.I)
        text = re.sub(r"température minimale\s*:\s*", "T° Min ", text, flags=re.I)
        text = re.sub(r"\bkmh\b", "km/h", text, flags=re.I)
        text = re.sub(r"\b(vent|rafales|humidité)\s*:\s*", r"\1 ", text, flags=re.I)
        clauses = [p.strip(" .;|") for p in re.split(r"[;|]|(?=T° (?:Max|Min)|\bvent |\brafales |\bhumidité )", text) if p.strip(" .;|")]
        answer = ""
        for clause in clauses:
            candidate = (answer + ", " if answer else "") + clause
            if len((candidate + ".").encode("utf-8")) <= budget:
                answer = candidate
        return answer + "." if answer else "Prévision trop longue pour un message. Précise température, vent ou pluie."

    async def _weather_tool(self, question: str, message: MeshMessage, args: dict | None = None) -> str:
        """Invoke wx as an internal ASK capability, even when direct wx is hidden."""
        from ..commands.wx_command import WxCommand

        command = self._command("wx")
        if type(command) is not WxCommand or not self._allowed(command, message, service=True):
            return "Je ne peux pas te donner la météo pour le moment. Réessaie dans quelques instants."
        default_location = self.owner.bot.config.get(
            "Weather", "default_city", fallback=""
        ).strip()
        default_display_location = self.owner.bot.config.get(
            "Weather", "default_location_label", fallback=default_location
        ).strip() or default_location
        command_without_default = self._weather_command(question)
        if args:
            command_without_default = " ".join(p for p in ("wx", args["location"], "tomorrow" if args["period"] == "tomorrow" else "") if p)
        uses_default_location = bool(default_location) and command_without_default in {
            "wx", "wx tomorrow"
        }
        async with self._rf_lock:
            cloned = deepcopy(message)
            cloned.content = command_without_default
            if uses_default_location:
                cloned.content = " ".join(p for p in ("wx", default_location, "tomorrow" if "tomorrow" in command_without_default else "") if p)
            self.record_function("weather.forecast")
            command.record_execution(message.sender_id or None)
            cloned.content_lower = cloned.content.casefold()
            cloned.prefix_normalized = True
            cloned.capture_sink = []
            await command.execute(cloned)
            if not cloned.capture_sink:
                return "Le service météo n'a renvoyé aucune prévision."
            raw_answer = "\n".join(cloned.capture_sink)
            if uses_default_location:
                raw_answer = self._replace_weather_location(
                    raw_answer, default_display_location
                )

        # Tool data remains authoritative. The LLM only turns the compact wx
        # notation into natural language; any error or altered value falls back
        # to the original forecast.
        source_location = self._weather_source_location(raw_answer)
        llm = self._command("llm")
        if self._allowed(llm, message, service=True):
            provider = getattr(command, "delegate_command", None)
            wind_unit = getattr(provider, "wind_speed_unit", "")
            wind_label = wind_unit or "l'unité indiquée par la source"
            requested_answer = self._select_weather_period(question, raw_answer)
            labelled_answer = self._expand_weather_notation(requested_answer, wind_unit)
            location_context = (
                f"Le lieu à afficher est exactement « {source_location} » : "
                "conserve-le et n'en invente aucun autre. "
                if source_location else ""
            )
            context = (
                "H signifie température maximale et L température minimale. "
                "La température actuelle est une mesure distincte : ne la présente jamais "
                "comme une minimale ou une maximale. "
                f"Une notation comme 17G36 signifie vent 17 et rafales 36 en {wind_label}. "
                "Ces valeurs ne désignent jamais une température intérieure ou extérieure. "
                "Réponds seulement pour la période demandée, dans une formulation naturelle. "
                "Présente les températures minimale et maximale comme une plage. "
                "Commence par le lieu et la période des données météo. "
                + location_context
                +
                "Aucun emoji, aucune liste de champs, aucun code météo brut, "
                "aucun point de rosée, visibilité ou pression. Un seul message, sans expliquer "
                f"les abréviations. Budget maximal : {self.owner.get_max_message_length(message)} octets UTF-8."
            )
            reformulated = await llm.service.rephrase_tool_result(
                question,
                labelled_answer,
                message=message,
                context=context,
                max_length=self.owner.get_max_message_length(message),
            )
            if reformulated and source_location:
                from .catalog import grounded_place
                heading = reformulated.partition(":")[0]
                if not grounded_place(source_location, heading):
                    self.owner.logger.warning("Weather reformulation changed or omitted source location")
                    reformulated = None
            if reformulated and "humidité" in reformulated.casefold() and "humidité" not in labelled_answer.casefold():
                self.owner.logger.warning("Weather reformulation confused rain probability with humidity")
                reformulated = None
            if reformulated:
                if uses_default_location:
                    reformulated = self._enforce_weather_location(
                        reformulated, default_display_location
                    )
                return split_reply(
                    reformulated,
                    self.owner.get_max_message_length(message),
                    max_pages=1,
                )[0]
        return self._weather_fallback(
            self._expand_weather_notation(self._select_weather_period(question, raw_answer), getattr(getattr(command, "delegate_command", None), "wind_speed_unit", "")),
            self.owner.get_max_message_length(message),
        )
