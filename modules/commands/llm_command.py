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
from ..solar_conditions import get_moon, get_sun
from ..utils import geocode_city_sync, get_cpu_temperature
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
        self.context_include_contacts = self.get_config_value(
            "Llm_Command", "context_include_contacts", fallback=True, value_type="bool"
        )
        self.context_include_moon = self.get_config_value(
            "Llm_Command", "context_include_moon", fallback=True, value_type="bool"
        )
        self.context_include_sun = self.get_config_value(
            "Llm_Command", "context_include_sun", fallback=True, value_type="bool"
        )
        self.context_include_commands = self.get_config_value(
            "Llm_Command", "context_include_commands", fallback=True, value_type="bool"
        )
        self.context_cache_seconds = self.get_config_value(
            "Llm_Command", "context_cache_seconds", fallback=60, value_type="int"
        )
        # Weather location for LLM context (defaults to Paris, France)
        self.context_weather_location = self.get_config_value(
            "Llm_Command", "context_weather_location", fallback="Paris, France", value_type="str"
        )
        self._cached_context_str = ""
        self._cached_context_time = 0.0
        self._cached_commands_list = None

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

    def _get_enabled_commands_list(self) -> list[dict[str, Any]]:
        """Get list of enabled bot commands with their keywords and descriptions.

        Returns:
            List of command dicts with 'name', 'keywords', and 'description' keys.
        """
        if self._cached_commands_list is not None:
            return self._cached_commands_list

        try:
            from modules.plugin_loader import PluginLoader  # noqa: PLC0415

            # Load all plugins using the bot's plugin loader
            plugin_loader = PluginLoader(self.bot)
            commands = plugin_loader.load_all_plugins()

            # Get admin commands to exclude them
            admin_commands_str = self.bot.config.get('Admin_ACL', 'admin_commands', fallback='')
            admin_commands = {c.strip() for c in admin_commands_str.split(',') if c.strip()}

            # Filter to only enabled, non-admin commands
            enabled_commands = []
            for cmd_name, cmd_instance in commands.items():
                # Skip admin commands
                primary_name = getattr(cmd_instance, 'name', cmd_name)
                if cmd_name in admin_commands or primary_name in admin_commands:
                    continue
                if hasattr(cmd_instance, 'requires_admin_access') and cmd_instance.requires_admin_access():
                    continue

                # Check if command is enabled
                if not self._is_command_enabled(cmd_instance):
                    continue

                # Get command info
                keywords = getattr(cmd_instance, 'keywords', [])
                if not keywords:
                    continue

                enabled_commands.append({
                    'name': primary_name,
                    'keywords': keywords,
                    'description': getattr(cmd_instance, 'short_description', None) or getattr(cmd_instance, 'description', ''),
                })

            # Sort by name
            enabled_commands.sort(key=lambda c: str(c['name']))
            self._cached_commands_list = enabled_commands
            return enabled_commands
        except Exception as e:
            self.logger.warning(f"Failed to load commands list for LLM context: {e}")
            return []

    @staticmethod
    def _is_command_enabled(cmd_instance: Any) -> bool:
        """Return True if the command is currently enabled in configuration."""
        name = getattr(cmd_instance, 'name', '')
        if name:
            named_attr = f"{name}_enabled"
            if hasattr(cmd_instance, named_attr):
                return bool(getattr(cmd_instance, named_attr))
        if hasattr(cmd_instance, 'enabled'):
            return bool(cmd_instance.enabled)
        return True

    def _build_local_context(self) -> str:
        """Build a local context string with contacts, moon, sun, and weather info.

        Returns:
            String containing formatted local context, or empty string if disabled or cached.
        """
        if not self.include_local_context:
            return ""

        # Check cache
        now = time.time()
        if self._cached_context_str and (now - self._cached_context_time) < self.context_cache_seconds:
            return self._cached_context_str

        context_parts = []

        # Add contacts statistics
        if self.context_include_contacts:
            try:
                with self.bot.db_manager.connection() as conn:
                    cursor = conn.cursor()
                    # Count total contacts
                    cursor.execute("SELECT COUNT(*) FROM contacts")
                    total_contacts = cursor.fetchone()[0]
                    # Count contacts seen in last 24 hours
                    cursor.execute(
                        "SELECT COUNT(*) FROM contacts WHERE last_seen >= ?",
                        (int(time.time()) - 86400,)
                    )
                    recent_contacts = cursor.fetchone()[0]
                    if total_contacts > 0:
                        context_parts.append(f"Contacts: {total_contacts} total, {recent_contacts} active (24h)")
            except Exception as e:
                self.logger.warning(f"Failed to get contacts stats: {e}")

        # Add moon information
        if self.context_include_moon:
            try:
                moon_info = get_moon()
                if moon_info and "Error" not in moon_info:
                    # Extract just the phase and illumination
                    lines = moon_info.split('\n')
                    for line in lines:
                        if line.startswith("Phase:"):
                            phase_info = line.replace("Phase:", "").strip()
                            context_parts.append(f"Moon: {phase_info}")
                            break
            except Exception as e:
                self.logger.warning(f"Failed to get moon info: {e}")

        # Add sun information
        if self.context_include_sun:
            try:
                sun_info = get_sun()
                if sun_info and "Error" not in sun_info:
                    # Extract sunrise/sunset
                    lines = sun_info.split('\n')
                    if lines:
                        context_parts.append(f"Sun: {lines[0]}")
            except Exception as e:
                self.logger.warning(f"Failed to get sun info: {e}")

        # Add weather for configured location
        if self.context_include_weather and self.context_weather_location:
            try:
                # Try to get weather using the wx command logic
                weather_info = self._get_weather_for_location(self.context_weather_location)
                if weather_info:
                    context_parts.append(f"Weather ({self.context_weather_location}): {weather_info}")
            except Exception as e:
                self.logger.warning(f"Failed to get weather info: {e}")

        # Add available bot commands
        if self.context_include_commands:
            try:
                commands = self._get_enabled_commands_list()
                if commands:
                    # Get command prefix for display
                    command_prefix = self.bot.config.get('Bot', 'command_prefix', fallback='').strip()

                    # Build commands list string
                    commands_list = []
                    for cmd in commands:
                        # Show first 3 keywords as examples
                        keywords = cmd['keywords'][:3]
                        keyword_examples = ', '.join([f"{command_prefix}{kw}" for kw in keywords])
                        commands_list.append(f"  - {cmd['name']}: {cmd['description']} (e.g., {keyword_examples})")

                    commands_str = "Available Commands:\n" + "\n".join(commands_list)
                    context_parts.append(commands_str)
            except Exception as e:
                self.logger.warning(f"Failed to get commands list: {e}")

        # Build final context string
        if context_parts:
            self._cached_context_str = "\n".join(context_parts)
            self._cached_context_time = now
            return self._cached_context_str

        return ""

    def _get_weather_for_location(self, location: str) -> str:
        """Get weather information for a specific location.

        Args:
            location: City name or location string (e.g., "Paris, France")

        Returns:
            Formatted weather string, or empty string if unavailable
        """
        try:
            # Try to geocode the location
            # Let geocode_city_sync handle country detection from the location string
            lat, lon, _ = geocode_city_sync(self.bot, location)
            if lat is None or lon is None:
                return ""

            # Use Open-Meteo API for international weather
            url = "https://api.open-meteo.com/v1/forecast"
            params = {
                "latitude": lat,
                "longitude": lon,
                "current": "temperature_2m,weather_code,wind_speed_10m",
                "temperature_unit": "celsius",
                "wind_speed_unit": "kmh",
                "timezone": "auto"
            }

            response = requests.get(url, params=params, timeout=5)
            if response.status_code == 200:
                data = response.json()
                current = data.get("current", {})
                temp = current.get("temperature_2m")
                wind = current.get("wind_speed_10m")
                weather_code = current.get("weather_code", 0)

                # Simple weather code description mapping
                weather_desc = self._get_weather_description(weather_code)

                if temp is not None:
                    return f"{temp}°C, {weather_desc}, Wind: {wind}km/h"

            return ""
        except Exception as e:
            self.logger.warning(f"Failed to fetch weather for {location}: {e}")
            return ""

    def _get_weather_description(self, code: int) -> str:
        """Convert WMO weather code to simple description."""
        if code == 0:
            return "Clear"
        elif code in [1, 2, 3]:
            return "Partly Cloudy"
        elif code in [45, 48]:
            return "Foggy"
        elif code in [51, 53, 55, 61, 63, 65, 80, 81, 82]:
            return "Rainy"
        elif code in [71, 73, 75, 77, 85, 86]:
            return "Snowy"
        elif code in [95, 96, 99]:
            return "Thunderstorm"
        else:
            return "Variable"

    def _inject_current_time_into_prompt(self, prompt: str) -> str:
        """Inject the current system time and local context into a system prompt.

        Uses the server's local time zone without explicit timezone conversion,
        as the timezone config option was removed for simplification.
        """
        try:
            current_time = datetime.now().strftime(self.datetime_format)
            result = f"{prompt}\n[Current time: {current_time}]"

            # Add local context if enabled
            local_context = self._build_local_context()
            if local_context:
                result += f"\n[Local Context:\n{local_context}]"

            return result
        except Exception as e:
            self.logger.warning(f"Error injecting current time/context: {e}")
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

        # Build the payload with current time and context injected in system prompt
        payload = self._build_payload(prompt=prompt, history=history)

        try:
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
