"""Tests for the validate_config.py CLI entry point, in particular --strict."""

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

VALID_CONFIG = """[Connection]
connection_type = serial
serial_port = /dev/ttyUSB0

[Bot]
bot_name = TestBot
db_path = {db_path}

[Channels]
monitor_channels = general
respond_to_dms = true

[Keywords]
test = ack
"""

CONFIG_WITH_UNKNOWN_SECTION = VALID_CONFIG + """
[TotallyMadeUpSection]
foo = bar
"""


def _run_validate_config(config_path: Path, *extra_args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "validate_config.py", "--config", str(config_path), *extra_args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )


class TestValidateConfigCli:
    def test_unknown_section_does_not_fail_without_strict(self, tmp_path):
        config = tmp_path / "config.ini"
        config.write_text(CONFIG_WITH_UNKNOWN_SECTION.format(db_path=str(tmp_path / "meshcore_bot.db")))
        result = _run_validate_config(config)
        assert result.returncode == 0
        assert "Unknown section [TotallyMadeUpSection]" in result.stderr

    def test_unknown_section_fails_with_strict(self, tmp_path):
        config = tmp_path / "config.ini"
        config.write_text(CONFIG_WITH_UNKNOWN_SECTION.format(db_path=str(tmp_path / "meshcore_bot.db")))
        result = _run_validate_config(config, "--strict")
        assert result.returncode == 1
        assert "Unknown section [TotallyMadeUpSection]" in result.stderr

    def test_valid_config_passes_with_strict(self, tmp_path):
        config = tmp_path / "config.ini"
        config.write_text(VALID_CONFIG.format(db_path=str(tmp_path / "meshcore_bot.db")))
        result = _run_validate_config(config, "--strict")
        assert result.returncode == 0

    def test_error_still_fails_regardless_of_strict(self, tmp_path):
        config = tmp_path / "config.ini"
        config.write_text("[Bot]\nbot_name = Test\n")
        result = _run_validate_config(config)
        assert result.returncode == 1
