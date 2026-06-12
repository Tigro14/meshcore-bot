#!/usr/bin/env python3
"""
LLM command for the MeshCore Bot.
Sends a short prompt to a local llama.cpp OpenAI-compatible endpoint.
"""

import asyncio
import re
import time
from datetime import datetime
from typing import Any

import requests

from ..models import MeshMessage
from ..utils import get_config_timezone, get_cpu_temperature
from .base_command import BaseCommand


class LlmCommand(BaseCommand):
    """Handles llm command for local llama.cpp chat responses."""

    name = "llm"
    keywords = ["llm", "ia", "ai", "chat"]
    description = "Chat with local llama.cpp AI (ask a short question)"
    category = "basic"
    cooldown_seconds = 5

    short_description = "Ask the local llama.cpp model a short question"
    usage = "llm <question>"
    examples = ["llm What is APRS?", "llm summarize LoRa in one sentence"]
    parameters = [
        {"name": "question", "description": "Prompt to send to local llama.cpp"}
    ]

    def __init__(self, bot):
        super().__init__(bot)
        self.llm_enabled = self.get_config_value("Llm_Command", "enabled", fallback=False, value_type="bool")
        self.endpoint = self.get_config_value(
            "Llm_Command",
            "endpoint",
            fallback="http://127.0.0.1:8080/v1/chat/completions",
            value_type="str",
        )
        self.model = self.get_config_value("Llm_Command", "model", fallback="", value_type="str")
        self.system_prompt = self.get_config_value(
            "Llm_Command",
            "system_prompt",
            fallback="You are a helpful assistant on a low-bandwidth mesh network. Reply briefly in one short sentence.",
            value_type="str",
        )
        self.timeout_seconds = max(
            1.0,
            min(
                120.0,
                self.get_config_value("Llm_Command", "timeout_seconds", fallback=20.0, value_type="float"),
            ),
        )
        self.max_tokens = max(
            8,
            min(
                512,
                self.get_config_value("Llm_Command", "max_tokens", fallback=80, value_type="int"),
            ),
        )
        self.temperature = max(
            0.0,
            min(
                2.0,
                self.get_config_value("Llm_Command", "temperature", fallback=0.4, value_type="float"),
            ),
        )
        self.top_p = max(
            0.0,
            min(
                1.0,
                self.get_config_value("Llm_Command", "top_p", fallback=0.9, value_type="float"),
            ),
        )
        self.strip_thinking_tags = self.get_config_value(
            "Llm_Command",
            "strip_thinking_tags",
            fallback=True,
            value_type="bool",
        )
        self.context_window_seconds = max(
            0,
            self.get_config_value("Llm_Command", "context_window_seconds", fallback=600, value_type="int"),
        )
        self.context_max_turns = max(
            1,
            min(
                20,
                self.get_config_value("Llm_Command", "context_max_turns", fallback=5, value_type="int"),
            ),
        )
        # Pagination settings
        self.pagination_enabled = self.get_config_value(
            "Llm_Command",
            "pagination_enabled",
            fallback=False,
            value_type="bool",
        )
        self.page_count = max(
            1,
            min(
                10,
                self.get_config_value("Llm_Command", "page_count", fallback=2, value_type="int"),
            ),
        )
        self.chars_per_page = max(
            50,
            min(
                500,
                self.get_config_value("Llm_Command", "chars_per_page", fallback=160, value_type="int"),
            ),
        )

        # Context settings
        self.include_local_context = self.get_config_value(
            "Llm_Command", "include_local_context", fallback=True, value_type="bool"
        )
        self.context_include_weather = self.get_config_value(
            "Llm_Command", "context_include_weather", fallback=True, value_type="bool"
        )
        self.context_include_repeaters = self.get_config_value(
            "Llm_Command", "context_include_repeaters", fallback=True, value_type="bool"
        )
        self.context_include_network_status = self.get_config_value(
            "Llm_Command", "context_include_network_status", fallback=True, value_type="bool"
        )
        self.context_cache_seconds = self.get_config_value(
            "Llm_Command", "context_cache_seconds", fallback=60, value_type="int"
        )
        self._cached_context_str = ""
        self._cached_context_time = 0.0

        # CPU temperature cooling threshold (in degrees Celsius)
        self.cpu_temp_threshold = max(
            0.0,
            min(
                100.0,
                self.get_config_value("Llm_Command", "cpu_temp_threshold", fallback=60.0, value_type="float"),
            ),
        )
        # Datetime format for current time injection
        self.datetime_format = "%Y-%m-%d %H:%M:%S"
        # Per-user conversation history: {user_key: [{"role": str, "content": str, "ts": float}]}
        self._context: dict[str, list[dict[str, Any]]] = {}

    def can_execute(self, message: MeshMessage, skip_channel_check: bool = False) -> bool:
        if not self.llm_enabled:
            return False

        # Check CPU temperature threshold if configured
        if self.cpu_temp_threshold > 0:
            cpu_temp = get_cpu_temperature()
            if cpu_temp is not None and cpu_temp >= self.cpu_temp_threshold:
                self.logger.info(
                    f"LLM command blocked: CPU temperature {cpu_temp:.1f}°C exceeds threshold {self.cpu_temp_threshold}°C"
                )
                return False

        return super().can_execute(message, skip_channel_check)

    def get_help_text(self) -> str:
        pfx = self._command_prefix
        return f"Usage: {pfx}llm <question> - Ask local llama.cpp for a short reply"

    def _user_key(self, message: MeshMessage) -> str | None:
        """Return a stable key for per-user context tracking, or None if unavailable."""
        return message.sender_pubkey or message.sender_id or None

    def _get_context_history(self, user_key: str) -> list[dict[str, str]]:
        """Return cleaned conversation history for *user_key*, pruning expired entries."""
        if self.context_window_seconds <= 0:
            return []

        entries = self._context.get(user_key, [])
        if not entries:
            return []

        cutoff = time.time() - self.context_window_seconds
        fresh = [e for e in entries if e["ts"] >= cutoff]

        # Keep only the most recent context_max_turns complete turns (2 messages each)
        max_messages = self.context_max_turns * 2
        if len(fresh) > max_messages:
            fresh = fresh[-max_messages:]

        self._context[user_key] = fresh
        return [{"role": e["role"], "content": e["content"]} for e in fresh]

    def _store_context(self, user_key: str, prompt: str, reply: str) -> None:
        """Append a new user/assistant turn to the context store."""
        if self.context_window_seconds <= 0:
            return

        now = time.time()
        entries = self._context.setdefault(user_key, [])
        entries.append({"role": "user", "content": prompt, "ts": now})
        entries.append({"role": "assistant", "content": reply, "ts": now})

    def _extract_prompt(self, message: MeshMessage) -> str:
        content = message.content.strip()

        if self._command_prefix:
            if content.startswith(self._command_prefix):
                content = content[len(self._command_prefix):].strip()
        elif content.startswith("!"):
            # Backward-compatibility: base_command.matches_keyword also strips a leading
            # "!" when no command_prefix is configured, so we do the same here.
            content = content[1:].strip()

        content = self._strip_mentions(content)
        lowered = content.lower()

        for keyword in sorted(self.keywords, key=len, reverse=True):
            kw = keyword.lower()
            if lowered == kw:
                return ""
            if lowered.startswith(kw) and len(lowered) > len(kw) and lowered[len(kw)] == " ":
                return content[len(keyword):].strip()

        return ""

    def _inject_current_time_into_prompt(self, prompt: str) -> str:
        """Inject the current system time into a system prompt.

        Uses the server's local time zone without explicit timezone conversion,
        as the timezone config option was removed for simplification.
        """
        try:
            current_time = datetime.now().strftime(self.datetime_format)
            return f"{prompt}\n[Current time: {current_time}]"
        except Exception as e:
            self.logger.warning(f"Error injecting current time: {e}")
            return prompt

    def _build_payload(self, prompt: str = "", history: list[dict[str, str]] | None = None, messages: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """Build the API payload for the LLM request.

        This method supports two modes:
        1. Build from prompt + history: Call with prompt and optional history
        2. Use pre-built messages: Call with messages only (ignores prompt/history)

        Args:
            prompt: User prompt (used with history to build messages, ignored if messages provided)
            history: Conversation history (ignored if messages provided)
            messages: Pre-built messages list (takes precedence over prompt/history)

        Raises:
            ValueError: If called with messages parameter alongside non-empty prompt/history
        """
        # Validate that conflicting parameters aren't provided
        if messages is not None and (prompt or history):
            self.logger.warning("_build_payload: messages parameter provided with prompt/history; ignoring prompt/history")

        if messages is None:
            # Build messages from prompt and history
            system_prompt = self._inject_current_time_into_prompt(self.system_prompt)
            messages = [{"role": "system", "content": system_prompt}]
            if history:
                messages.extend(history)
            if prompt:
                messages.append({"role": "user", "content": prompt})

        payload: dict[str, Any] = {
            "messages": messages,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "top_p": self.top_p,
        }
        if self.model:
            payload["model"] = self.model

        return payload

    def _clean_ai_response(self, content: str, max_length: int) -> str:
        cleaned = content or ""
        if self.strip_thinking_tags:
            cleaned = re.sub(
                r"<(?:think|thinking)>.*?</(?:think|thinking)>",
                "",
                cleaned,
                flags=re.IGNORECASE | re.DOTALL,
            )
        cleaned = " ".join(cleaned.split()).strip()

        if not cleaned:
            cleaned = "No response from AI."

        if len(cleaned) > max_length:
            cleaned = cleaned[: max(0, max_length - 3)].rstrip()
            cleaned = (cleaned + "...") if cleaned else "..."

        return cleaned

    def _split_response_into_pages(self, content: str) -> list[str]:
        """Split a long response into multiple pages based on pagination settings.

        Args:
            content: The full response text to split.

        Returns:
            List of page strings, each respecting chars_per_page limit.
        """
        if not self.pagination_enabled or len(content) <= self.chars_per_page:
            return [content]

        pages = []
        words = content.split()
        current_page = ""
        word_idx = 0

        while word_idx < len(words):
            word = words[word_idx]
            # Check if adding this word would exceed the page limit
            test_page = (current_page + " " + word).strip() if current_page else word

            if len(test_page) <= self.chars_per_page:
                current_page = test_page
                word_idx += 1
            else:
                # Current page is full, save it and start a new one
                if current_page:
                    pages.append(current_page)
                    current_page = ""
                else:
                    # Single word exceeds limit, truncate it
                    pages.append(word[:self.chars_per_page - 3] + "...")
                    word_idx += 1

                # Check if we've reached the maximum page count
                if len(pages) >= self.page_count:
                    # Add remaining content indication if there are more words
                    if word_idx < len(words):
                        # Only truncate if needed to fit the marker
                        last_page = pages[-1]
                        marker = " [...]"
                        if len(last_page) + len(marker) > self.chars_per_page:
                            pages[-1] = last_page[:self.chars_per_page - len(marker)].rstrip() + marker
                        else:
                            pages[-1] = last_page + marker
                    return pages

        # Add the last page if there's content remaining
        if current_page:
            pages.append(current_page)

        return pages if pages else [content]

    async def execute(self, message: MeshMessage) -> bool:
        prompt = self._extract_prompt(message)
        if not prompt:
            pfx = self._command_prefix
            return await self.send_response(message, f"Usage: {pfx}llm <question>")

        user_key = self._user_key(message)
        history = self._get_context_history(user_key) if user_key else []

        # Build the payload with current time injected in system prompt
        payload = self._build_payload(prompt=prompt, history=history)

        try:
            payload = await self._build_payload(prompt, history)
            response = await asyncio.to_thread(
                requests.post,
                self.endpoint,
                json=payload,
                timeout=self.timeout_seconds,
            )
        except requests.RequestException as e:
            self.logger.warning(f"LLM command connection error: {e}")
            return await self.send_response(message, "LLM unavailable: local llama.cpp is unreachable.")

        if response.status_code != 200:
            self.logger.warning(f"LLM command error status: {response.status_code}")
            return await self.send_response(message, "LLM error: llama.cpp returned an invalid response.")

        try:
            data = response.json()
            choices = data.get("choices")
            if not isinstance(choices, list) or not choices:
                return await self.send_response(message, "LLM error: no response from model.")

            choice = choices[0]
            assistant_message = choice.get("message", {})
            content = assistant_message.get("content", "")

        except (ValueError, TypeError, IndexError, AttributeError, KeyError) as e:
            self.logger.warning(f"LLM command parse error: {e}")
            return await self.send_response(message, "LLM error: could not parse response.")

        # Clean the response first
        if self.pagination_enabled:
            # Use pagination: allow response up to total paginated capacity
            max_total_length = self.chars_per_page * self.page_count
            cleaned = self._clean_ai_response(content, max_total_length)
        else:
            # No pagination: truncate to single message max_length
            max_length = self.get_max_message_length(message)
            cleaned = self._clean_ai_response(content, max_length)

        if user_key:
            self._store_context(user_key, prompt, cleaned)

        # Split response into pages if pagination is enabled
        if self.pagination_enabled:
            pages = self._split_response_into_pages(cleaned)
            if len(pages) > 1:
                return await self.send_response_chunked(message, pages)

        return await self.send_response(message, cleaned)
