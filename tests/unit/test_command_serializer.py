"""Tests for the host->radio command serializer (_SerializedCommands).

These validate the core mitigation for the firmware corruption / parser-desync
failure mode: every command issued to the radio is serialized to one in-flight
companion frame at a time and paced by a minimum inter-command interval, so the
firmware's single-threaded serial loop cannot be overrun.
"""

import asyncio
import time
from pathlib import Path

import pytest

from modules.core import MeshCoreBot, _SerializedCommands


def _make_bot(tmp_path: Path, min_interval_ms: int = 30) -> MeshCoreBot:
    config_file = tmp_path / "config.ini"
    db_path = tmp_path / "bot.db"
    config_file.write_text(
        f"""[Connection]
connection_type = serial
serial_port = /dev/ttyUSB0
command_min_interval_ms = {min_interval_ms}

[Bot]
db_path = {db_path.as_posix()}
prefix_bytes = 1

[Channels]
monitor_channels = #general
""",
        encoding="utf-8",
    )
    return MeshCoreBot(config_file=str(config_file))


class FakeCommands:
    """Stand-in for meshcore.commands with an async command + passthrough attrs."""

    not_callable = 42

    def __init__(self):
        self.active = 0
        self.max_active = 0
        self.calls = 0

    async def do_work(self, delay: float = 0.02):
        self.calls += 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(delay)
            return "ok"
        finally:
            self.active -= 1

    def sync_method(self):
        return "sync"


class FakeMeshcore:
    def __init__(self):
        self.commands = FakeCommands()


def test_min_interval_parsed_from_config(tmp_path):
    bot = _make_bot(tmp_path, min_interval_ms=45)
    assert bot._radio_cmd_min_interval == pytest.approx(0.045)


def test_min_interval_negative_clamped_to_zero(tmp_path):
    bot = _make_bot(tmp_path, min_interval_ms=-100)
    assert bot._radio_cmd_min_interval == 0.0


async def test_serializes_concurrent_commands(tmp_path):
    """Only one wrapped command may run at a time."""
    bot = _make_bot(tmp_path, min_interval_ms=0)  # isolate mutex from pacing
    fake = FakeCommands()
    proxy = _SerializedCommands(bot, fake)

    await asyncio.gather(*(proxy.do_work(delay=0.01) for _ in range(10)))

    assert fake.calls == 10
    assert fake.max_active == 1  # never more than one in-flight frame


async def test_pacing_enforces_minimum_gap(tmp_path):
    bot = _make_bot(tmp_path, min_interval_ms=50)
    fake = FakeCommands()
    proxy = _SerializedCommands(bot, fake)

    start = time.monotonic()
    for _ in range(5):
        await proxy.do_work(delay=0.0)
    elapsed = time.monotonic() - start

    # 5 commands => 4 enforced gaps of ~50ms (first call is not delayed).
    assert elapsed >= 0.18


async def test_non_coroutine_attributes_pass_through(tmp_path):
    bot = _make_bot(tmp_path)
    fake = FakeCommands()
    proxy = _SerializedCommands(bot, fake)

    assert proxy.not_callable == 42
    assert proxy.sync_method() == "sync"


async def test_wrapped_command_returns_value(tmp_path):
    bot = _make_bot(tmp_path, min_interval_ms=0)
    fake = FakeCommands()
    proxy = _SerializedCommands(bot, fake)

    assert await proxy.do_work(delay=0.0) == "ok"


def test_install_command_serializer_wraps_and_is_idempotent(tmp_path):
    bot = _make_bot(tmp_path)
    bot.meshcore = FakeMeshcore()

    bot._install_command_serializer()
    wrapped = bot.meshcore.commands
    assert isinstance(wrapped, _SerializedCommands)

    # Re-installing must not double-wrap.
    bot._install_command_serializer()
    assert bot.meshcore.commands is wrapped


def test_install_command_serializer_noop_without_meshcore(tmp_path):
    bot = _make_bot(tmp_path)
    bot.meshcore = None
    bot._install_command_serializer()  # must not raise
    assert bot.meshcore is None
