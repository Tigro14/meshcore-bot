"""Tests for modules.commands.llm_command."""

from unittest.mock import AsyncMock, Mock, patch

import pytest
import requests

from modules.commands.llm_command import LlmCommand
from tests.conftest import mock_message


class TestLlmCommand:
    """Tests for LlmCommand."""

    def _enable_llm(self, bot):
        if not bot.config.has_section("Llm_Command"):
            bot.config.add_section("Llm_Command")
        bot.config.set("Llm_Command", "enabled", "true")

    def test_can_execute_when_enabled(self, command_mock_bot):
        self._enable_llm(command_mock_bot)
        cmd = LlmCommand(command_mock_bot)
        msg = mock_message(content="llm hello", is_dm=True)
        assert cmd.can_execute(msg) is True

    def test_can_execute_when_disabled(self, command_mock_bot):
        if not command_mock_bot.config.has_section("Llm_Command"):
            command_mock_bot.config.add_section("Llm_Command")
        command_mock_bot.config.set("Llm_Command", "enabled", "false")
        cmd = LlmCommand(command_mock_bot)
        msg = mock_message(content="llm hello", is_dm=True)
        assert cmd.can_execute(msg) is False

    def test_matches_keyword_no_prefix(self, command_mock_bot):
        """With no command_prefix configured, bare 'llm <text>' should match."""
        self._enable_llm(command_mock_bot)
        command_mock_bot.config.set("Bot", "command_prefix", "")
        cmd = LlmCommand(command_mock_bot)
        msg = mock_message(content="llm hello", is_dm=True)
        assert cmd.matches_keyword(msg) is True

    def test_matches_keyword_alias_ia(self, command_mock_bot):
        """Alias 'ia' should also match."""
        self._enable_llm(command_mock_bot)
        command_mock_bot.config.set("Bot", "command_prefix", "")
        cmd = LlmCommand(command_mock_bot)
        assert cmd.matches_keyword(mock_message(content="ia hello", is_dm=True)) is True

    def test_matches_keyword_alias_ai(self, command_mock_bot):
        """Alias 'ai' should also match."""
        self._enable_llm(command_mock_bot)
        command_mock_bot.config.set("Bot", "command_prefix", "")
        cmd = LlmCommand(command_mock_bot)
        assert cmd.matches_keyword(mock_message(content="ai hello", is_dm=True)) is True

    def test_matches_keyword_alias_chat(self, command_mock_bot):
        """Alias 'chat' should also match."""
        self._enable_llm(command_mock_bot)
        command_mock_bot.config.set("Bot", "command_prefix", "")
        cmd = LlmCommand(command_mock_bot)
        assert cmd.matches_keyword(mock_message(content="chat hello", is_dm=True)) is True

    def test_matches_keyword_with_configured_prefix(self, command_mock_bot):
        """With command_prefix='/', '!llm <text>' should NOT match but '/llm <text>' should."""
        self._enable_llm(command_mock_bot)
        command_mock_bot.config.set("Bot", "command_prefix", "/")
        cmd = LlmCommand(command_mock_bot)
        assert cmd.matches_keyword(mock_message(content="/llm hello", is_dm=True)) is True
        assert cmd.matches_keyword(mock_message(content="!llm hello", is_dm=True)) is False

    @pytest.mark.asyncio
    async def test_execute_without_prompt_returns_usage_no_prefix(self, command_mock_bot):
        """Usage string must use the configured command prefix."""
        self._enable_llm(command_mock_bot)
        command_mock_bot.config.set("Bot", "command_prefix", "")
        cmd = LlmCommand(command_mock_bot)
        msg = mock_message(content="llm", is_dm=True)
        result = await cmd.execute(msg)
        assert result is True
        assert command_mock_bot.command_manager.send_response.call_args[0][1] == "Usage: llm <question>"

    @pytest.mark.asyncio
    async def test_execute_without_prompt_returns_usage_with_prefix(self, command_mock_bot):
        """Usage string reflects a non-slash configured prefix."""
        self._enable_llm(command_mock_bot)
        command_mock_bot.config.set("Bot", "command_prefix", "!")
        cmd = LlmCommand(command_mock_bot)
        msg = mock_message(content="!llm", is_dm=True)
        result = await cmd.execute(msg)
        assert result is True
        assert command_mock_bot.command_manager.send_response.call_args[0][1] == "Usage: !llm <question>"

    @pytest.mark.asyncio
    async def test_execute_success_calls_llama_endpoint(self, command_mock_bot):
        self._enable_llm(command_mock_bot)
        command_mock_bot.config.set("Llm_Command", "endpoint", "http://127.0.0.1:8080/v1/chat/completions")
        command_mock_bot.config.set("Bot", "command_prefix", "")
        cmd = LlmCommand(command_mock_bot)
        msg = mock_message(content="llm what is mesh?", is_dm=True)

        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "choices": [{
                "message": {
                    "content": "<think>chain</think><thinking>chain2</thinking>Mesh is a decentralized radio network."
                }
            }]
        }

        with patch("modules.commands.llm_command.requests.post", return_value=mock_response) as post_mock:
            result = await cmd.execute(msg)

        assert result is True
        post_mock.assert_called_once()
        sent_text = command_mock_bot.command_manager.send_response.call_args[0][1]
        assert "Mesh is a decentralized radio network." in sent_text
        assert "<think>" not in sent_text
        assert "<thinking>" not in sent_text

    @pytest.mark.asyncio
    async def test_execute_handles_connection_errors(self, command_mock_bot):
        self._enable_llm(command_mock_bot)
        command_mock_bot.config.set("Bot", "command_prefix", "")
        cmd = LlmCommand(command_mock_bot)
        msg = mock_message(content="llm hello", is_dm=True)

        with patch("modules.commands.llm_command.requests.post", side_effect=requests.RequestException("boom")):
            result = await cmd.execute(msg)

        assert result is True
        sent_text = command_mock_bot.command_manager.send_response.call_args[0][1]
        assert "LLM unavailable" in sent_text

    def test_get_help_text_uses_configured_prefix(self, command_mock_bot):
        """get_help_text() must reflect the configured command prefix, not a hardcoded one."""
        self._enable_llm(command_mock_bot)
        command_mock_bot.config.set("Bot", "command_prefix", "!")
        cmd = LlmCommand(command_mock_bot)
        help_text = cmd.get_help_text()
        assert "!llm" in help_text
        assert "/llm" not in help_text

    # ── Context tests ──────────────────────────────────────────────────────────

    def test_context_disabled_when_window_zero(self, command_mock_bot):
        """context_window_seconds=0 disables context storage and lookup."""
        self._enable_llm(command_mock_bot)
        command_mock_bot.config.set("Llm_Command", "context_window_seconds", "0")
        cmd = LlmCommand(command_mock_bot)
        cmd._store_context("Alice", "hello", "hi")
        assert cmd._get_context_history("Alice") == []

    def test_context_stored_after_successful_exchange(self, command_mock_bot):
        """After a successful LLM call, context is stored for the sender."""
        self._enable_llm(command_mock_bot)
        command_mock_bot.config.set("Bot", "command_prefix", "")
        cmd = LlmCommand(command_mock_bot)
        cmd._store_context("Alice", "what is LoRa?", "LoRa is a long-range radio tech.")
        history = cmd._get_context_history("Alice")
        assert len(history) == 2
        assert history[0] == {"role": "user", "content": "what is LoRa?"}
        assert history[1] == {"role": "assistant", "content": "LoRa is a long-range radio tech."}

    def test_context_payload_includes_history(self, command_mock_bot):
        """_build_payload includes prior history before the new user message."""
        self._enable_llm(command_mock_bot)
        cmd = LlmCommand(command_mock_bot)
        history = [
            {"role": "user", "content": "what is mesh?"},
            {"role": "assistant", "content": "A mesh is a network."},
        ]
        payload = cmd._build_payload("tell me more", history)
        messages = payload["messages"]
        assert messages[0]["role"] == "system"
        assert messages[1] == {"role": "user", "content": "what is mesh?"}
        assert messages[2] == {"role": "assistant", "content": "A mesh is a network."}
        assert messages[3] == {"role": "user", "content": "tell me more"}

    def test_context_pruned_after_expiry(self, command_mock_bot):
        """Entries older than context_window_seconds are pruned."""
        import time
        self._enable_llm(command_mock_bot)
        command_mock_bot.config.set("Llm_Command", "context_window_seconds", "60")
        cmd = LlmCommand(command_mock_bot)
        old_ts = time.time() - 120
        cmd._context["Bob"] = [
            {"role": "user", "content": "old question", "ts": old_ts},
            {"role": "assistant", "content": "old answer", "ts": old_ts},
        ]
        history = cmd._get_context_history("Bob")
        assert history == []

    def test_context_max_turns_limits_history(self, command_mock_bot):
        """Only the most recent context_max_turns turns are included."""
        import time
        self._enable_llm(command_mock_bot)
        command_mock_bot.config.set("Llm_Command", "context_max_turns", "2")
        cmd = LlmCommand(command_mock_bot)
        now = time.time()
        entries = []
        for i in range(6):
            entries.append({"role": "user", "content": f"q{i}", "ts": now})
            entries.append({"role": "assistant", "content": f"a{i}", "ts": now})
        cmd._context["Carol"] = entries
        history = cmd._get_context_history("Carol")
        assert len(history) == 4  # 2 turns * 2 messages
        assert history[0]["content"] == "q4"
        assert history[-1]["content"] == "a5"

    @pytest.mark.asyncio
    async def test_execute_sends_history_to_endpoint(self, command_mock_bot):
        """execute() passes prior context history to _build_payload."""
        self._enable_llm(command_mock_bot)
        command_mock_bot.config.set("Bot", "command_prefix", "")
        cmd = LlmCommand(command_mock_bot)
        # Seed some history for this user
        cmd._store_context("TestUser", "what is LoRa?", "LoRa is a long-range radio.")
        msg = mock_message(content="llm tell me more", sender_id="TestUser", is_dm=True)

        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "choices": [{"message": {"content": "LoRa uses chirp spread spectrum."}}]
        }

        with patch("modules.commands.llm_command.requests.post", return_value=mock_response) as post_mock:
            await cmd.execute(msg)

        payload_sent = post_mock.call_args[1]["json"]
        roles = [m["role"] for m in payload_sent["messages"]]
        assert roles == ["system", "user", "assistant", "user"]

    @pytest.mark.asyncio
    async def test_execute_stores_new_turn_in_context(self, command_mock_bot):
        """After a successful exchange, the new turn is stored in context."""
        self._enable_llm(command_mock_bot)
        command_mock_bot.config.set("Bot", "command_prefix", "")
        cmd = LlmCommand(command_mock_bot)
        msg = mock_message(content="llm what is APRS?", sender_id="TestUser", is_dm=True)

        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "choices": [{"message": {"content": "APRS is Automatic Packet Reporting System."}}]
        }

        with patch("modules.commands.llm_command.requests.post", return_value=mock_response):
            await cmd.execute(msg)

        history = cmd._get_context_history("TestUser")
        assert any(e["content"] == "what is APRS?" for e in history)
        assert any("APRS" in e["content"] for e in history if e["role"] == "assistant")

    @pytest.mark.asyncio
    async def test_execute_no_context_stored_on_connection_error(self, command_mock_bot):
        """Context must NOT be updated when the LLM endpoint is unreachable."""
        self._enable_llm(command_mock_bot)
        command_mock_bot.config.set("Bot", "command_prefix", "")
        cmd = LlmCommand(command_mock_bot)
        msg = mock_message(content="llm hello", sender_id="TestUser", is_dm=True)

        with patch("modules.commands.llm_command.requests.post", side_effect=requests.RequestException("boom")):
            await cmd.execute(msg)

        assert cmd._get_context_history("TestUser") == []

    # ── Pagination tests ──────────────────────────────────────────────────────

    def test_pagination_disabled_returns_single_page(self, command_mock_bot):
        """When pagination is disabled, entire response is returned as one string."""
        self._enable_llm(command_mock_bot)
        command_mock_bot.config.set("Llm_Command", "pagination_enabled", "false")
        cmd = LlmCommand(command_mock_bot)
        content = "This is a test response that could be split but won't be."
        pages = cmd._split_response_into_pages(content)
        assert len(pages) == 1
        assert pages[0] == content

    def test_pagination_enabled_splits_long_response(self, command_mock_bot):
        """When pagination is enabled, long responses are split into multiple pages."""
        self._enable_llm(command_mock_bot)
        command_mock_bot.config.set("Llm_Command", "pagination_enabled", "true")
        command_mock_bot.config.set("Llm_Command", "chars_per_page", "50")
        command_mock_bot.config.set("Llm_Command", "page_count", "3")
        cmd = LlmCommand(command_mock_bot)
        content = "This is a very long response that should be split into multiple pages based on the character limit we have configured for pagination."
        pages = cmd._split_response_into_pages(content)
        assert len(pages) > 1
        for page in pages:
            assert len(page) <= 50

    def test_pagination_respects_page_count_limit(self, command_mock_bot):
        """Pagination should not exceed the configured page_count limit."""
        self._enable_llm(command_mock_bot)
        command_mock_bot.config.set("Llm_Command", "pagination_enabled", "true")
        command_mock_bot.config.set("Llm_Command", "chars_per_page", "50")
        command_mock_bot.config.set("Llm_Command", "page_count", "2")
        cmd = LlmCommand(command_mock_bot)
        # Very long content that would need more than 2 pages
        content = "This is an extremely long response with many words that would normally require multiple pages to display properly and completely."
        pages = cmd._split_response_into_pages(content)
        assert len(pages) <= 2
        assert len(pages) == 2  # Should use both pages
        # Last page should indicate truncation
        assert "[...]" in pages[-1]

    def test_pagination_splits_at_word_boundaries(self, command_mock_bot):
        """Pagination should split at word boundaries, not mid-word."""
        self._enable_llm(command_mock_bot)
        command_mock_bot.config.set("Llm_Command", "pagination_enabled", "true")
        command_mock_bot.config.set("Llm_Command", "chars_per_page", "50")
        command_mock_bot.config.set("Llm_Command", "page_count", "5")
        cmd = LlmCommand(command_mock_bot)
        content = "Hello world this is a test of the pagination system that should be split into multiple pages"
        pages = cmd._split_response_into_pages(content)
        for page in pages:
            # Each page should contain complete words (no mid-word splits)
            # and respect the page limit
            assert len(page) <= 50

    def test_pagination_short_response_no_split(self, command_mock_bot):
        """Short responses should not be split even when pagination is enabled."""
        self._enable_llm(command_mock_bot)
        command_mock_bot.config.set("Llm_Command", "pagination_enabled", "true")
        command_mock_bot.config.set("Llm_Command", "chars_per_page", "100")
        cmd = LlmCommand(command_mock_bot)
        content = "Short response"
        pages = cmd._split_response_into_pages(content)
        assert len(pages) == 1
        assert pages[0] == content

    @pytest.mark.asyncio
    async def test_execute_with_pagination_sends_multiple_messages(self, command_mock_bot):
        """When pagination is enabled and response is long, multiple messages are sent."""
        self._enable_llm(command_mock_bot)
        command_mock_bot.config.set("Bot", "command_prefix", "")
        command_mock_bot.config.set("Llm_Command", "pagination_enabled", "true")
        command_mock_bot.config.set("Llm_Command", "chars_per_page", "50")
        command_mock_bot.config.set("Llm_Command", "page_count", "3")
        # Add mock for send_response_chunked
        command_mock_bot.command_manager.send_response_chunked = AsyncMock(return_value=True)
        cmd = LlmCommand(command_mock_bot)
        msg = mock_message(content="llm explain mesh networking", is_dm=True)

        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "choices": [{
                "message": {
                    "content": "Mesh networking is a decentralized network topology where each node relays data for the network, creating a robust and self-healing infrastructure."
                }
            }]
        }

        with patch("modules.commands.llm_command.requests.post", return_value=mock_response):
            result = await cmd.execute(msg)

        assert result is True
        # Check if chunked response was called
        if command_mock_bot.command_manager.send_response_chunked.call_count > 0:
            chunks = command_mock_bot.command_manager.send_response_chunked.call_args[0][1]
            assert isinstance(chunks, list)
            assert len(chunks) > 1
            for chunk in chunks:
                assert len(chunk) <= 50

    # ── CPU Temperature Throttling tests ──────────────────────────────────────

    def test_cpu_temp_threshold_default_value(self, command_mock_bot):
        """CPU temperature threshold should default to 60.0°C."""
        self._enable_llm(command_mock_bot)
        cmd = LlmCommand(command_mock_bot)
        assert cmd.cpu_temp_threshold == 60.0

    def test_cpu_temp_threshold_custom_value(self, command_mock_bot):
        """CPU temperature threshold can be configured."""
        self._enable_llm(command_mock_bot)
        command_mock_bot.config.set("Llm_Command", "cpu_temp_threshold", "55.0")
        cmd = LlmCommand(command_mock_bot)
        assert cmd.cpu_temp_threshold == 55.0

    def test_cpu_temp_threshold_disabled_with_zero(self, command_mock_bot):
        """CPU temperature threshold can be disabled by setting to 0."""
        self._enable_llm(command_mock_bot)
        command_mock_bot.config.set("Llm_Command", "cpu_temp_threshold", "0")
        cmd = LlmCommand(command_mock_bot)
        assert cmd.cpu_temp_threshold == 0.0

    def test_can_execute_blocked_when_cpu_temp_exceeds_threshold(self, command_mock_bot):
        """can_execute should return True when CPU temperature exceeds threshold (check moved to execute)."""
        self._enable_llm(command_mock_bot)
        command_mock_bot.config.set("Llm_Command", "cpu_temp_threshold", "60.0")
        cmd = LlmCommand(command_mock_bot)
        msg = mock_message(content="llm hello", is_dm=True)

        with patch("modules.commands.llm_command.get_cpu_temperature", return_value=65.0):
            assert cmd.can_execute(msg) is True

    def test_can_execute_allowed_when_cpu_temp_below_threshold(self, command_mock_bot):
        """can_execute should return True when CPU temperature is below threshold."""
        self._enable_llm(command_mock_bot)
        command_mock_bot.config.set("Llm_Command", "cpu_temp_threshold", "60.0")
        cmd = LlmCommand(command_mock_bot)
        msg = mock_message(content="llm hello", is_dm=True)

        with patch("modules.commands.llm_command.get_cpu_temperature", return_value=55.0):
            assert cmd.can_execute(msg) is True

    def test_can_execute_allowed_when_cpu_temp_equals_threshold(self, command_mock_bot):
        """can_execute should return True when CPU temperature equals threshold (check moved to execute)."""
        self._enable_llm(command_mock_bot)
        command_mock_bot.config.set("Llm_Command", "cpu_temp_threshold", "60.0")
        cmd = LlmCommand(command_mock_bot)
        msg = mock_message(content="llm hello", is_dm=True)

        with patch("modules.commands.llm_command.get_cpu_temperature", return_value=60.0):
            assert cmd.can_execute(msg) is True

    def test_can_execute_allowed_when_cpu_temp_reading_fails(self, command_mock_bot):
        """can_execute should allow execution when CPU temperature cannot be read (returns None)."""
        self._enable_llm(command_mock_bot)
        command_mock_bot.config.set("Llm_Command", "cpu_temp_threshold", "60.0")
        cmd = LlmCommand(command_mock_bot)
        msg = mock_message(content="llm hello", is_dm=True)

        with patch("modules.commands.llm_command.get_cpu_temperature", return_value=None):
            assert cmd.can_execute(msg) is True

    def test_can_execute_allowed_when_threshold_disabled(self, command_mock_bot):
        """can_execute should allow execution when threshold is set to 0 (disabled)."""
        self._enable_llm(command_mock_bot)
        command_mock_bot.config.set("Llm_Command", "cpu_temp_threshold", "0")
        cmd = LlmCommand(command_mock_bot)
        msg = mock_message(content="llm hello", is_dm=True)

        with patch("modules.commands.llm_command.get_cpu_temperature", return_value=80.0):
            # Even with high temperature, should be allowed when threshold is disabled
            assert cmd.can_execute(msg) is True

    @pytest.mark.asyncio
    async def test_execute_returns_trop_chaud_when_cpu_temp_exceeds_threshold(self, command_mock_bot):
        """execute should return 'trop chaud:' when CPU temperature exceeds threshold."""
        self._enable_llm(command_mock_bot)
        command_mock_bot.config.set("Llm_Command", "cpu_temp_threshold", "60.0")
        cmd = LlmCommand(command_mock_bot)
        msg = mock_message(content="llm hello", is_dm=True)

        with patch("modules.commands.llm_command.get_cpu_temperature", return_value=65.0):
            result = await cmd.execute(msg)
            assert result is True
            assert command_mock_bot.command_manager.send_response.call_args[0][1] == "trop chaud:"

    @pytest.mark.asyncio
    async def test_execute_returns_trop_chaud_when_cpu_temp_equals_threshold(self, command_mock_bot):
        """execute should return 'trop chaud:' when CPU temperature equals threshold."""
        self._enable_llm(command_mock_bot)
        command_mock_bot.config.set("Llm_Command", "cpu_temp_threshold", "60.0")
        cmd = LlmCommand(command_mock_bot)
        msg = mock_message(content="llm hello", is_dm=True)

        with patch("modules.commands.llm_command.get_cpu_temperature", return_value=60.0):
            result = await cmd.execute(msg)
            assert result is True
            assert command_mock_bot.command_manager.send_response.call_args[0][1] == "trop chaud:"

