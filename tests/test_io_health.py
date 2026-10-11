"""Tests for read-only filesystem detection and reboot signalling."""

from __future__ import annotations

import configparser
import errno
import logging
from pathlib import Path

import pytest

from modules.io_health import IOHealthMonitor


def erofs(path: str) -> OSError:
    return OSError(errno.EROFS, "Read-only file system", path)


@pytest.fixture
def config():
    parser = configparser.ConfigParser()
    parser.add_section("IO_Health")
    parser.set("IO_Health", "enabled", "true")
    parser.set("IO_Health", "reboot_on_erofs", "true")
    parser.set("IO_Health", "failure_threshold", "3")
    parser.set("IO_Health", "cooldown_seconds", "900")
    parser.set("IO_Health", "writable_prefixes", "/var/lib/meshcore-bot,/dev/shm")
    return parser


@pytest.fixture
def monitor(config, tmp_path):
    return IOHealthMonitor(config, logging.getLogger("test_io_health"), semaphore_dir=str(tmp_path))


class TestMonitoredPaths:
    def test_state_path_is_monitored(self, monitor):
        assert monitor.is_monitored_path("/var/lib/meshcore-bot/data/wiki_rag/wiki_pages.jsonl")

    def test_sandbox_path_is_not_monitored(self, monitor):
        assert not monitor.is_monitored_path("/opt/meshcore-bot/data/wiki_rag/wiki_pages.jsonl")

    def test_non_erofs_failure_is_ignored(self, monitor):
        assert not monitor.report_write_failure(OSError(errno.EACCES, "Permission denied", "/var/lib/meshcore-bot/x"), "/var/lib/meshcore-bot/x")
        assert monitor.consecutive_failures == 0

    def test_erofs_outside_prefixes_is_ignored(self, monitor):
        assert not monitor.report_write_failure(erofs("/opt/meshcore-bot/data/wiki_rag/wiki_pages.jsonl"), "/opt/meshcore-bot/data/wiki_rag/wiki_pages.jsonl")
        assert monitor.consecutive_failures == 0
        assert monitor.total_failures == 1


class TestThresholdAndSemaphore:
    def test_threshold_raises_semaphore(self, monitor, tmp_path):
        for _ in range(monitor.failure_threshold - 1):
            assert not monitor.report_write_failure(erofs("/var/lib/meshcore-bot/x"), "/var/lib/meshcore-bot/x")
        assert monitor.report_write_failure(erofs("/var/lib/meshcore-bot/x"), "/var/lib/meshcore-bot/x")
        info = Path(tmp_path) / "meshbot_reboot.info"
        assert "IOHealthWatchdog" in info.read_text(encoding="utf-8")

    def test_cooldown_blocks_second_signal(self, monitor, tmp_path):
        for _ in range(monitor.failure_threshold):
            monitor.report_write_failure(erofs("/var/lib/meshcore-bot/x"), "/var/lib/meshcore-bot/x")
        monitor._lock_fd = None
        assert not monitor.report_write_failure(erofs("/var/lib/meshcore-bot/y"), "/var/lib/meshcore-bot/y")
        assert monitor._lock_fd is None

    def test_disabled_monitor_does_nothing(self, config, tmp_path):
        config.set("IO_Health", "enabled", "false")
        monitor = IOHealthMonitor(config, logging.getLogger("test_io_health"), semaphore_dir=str(tmp_path))
        for _ in range(monitor.failure_threshold):
            assert not monitor.report_write_failure(erofs("/var/lib/meshcore-bot/x"), "/var/lib/meshcore-bot/x")
        assert not (Path(tmp_path) / "meshbot_reboot.info").exists()

    def test_reboot_on_erofs_false_does_not_signal(self, config, tmp_path):
        config.set("IO_Health", "reboot_on_erofs", "false")
        monitor = IOHealthMonitor(config, logging.getLogger("test_io_health"), semaphore_dir=str(tmp_path))
        for _ in range(monitor.failure_threshold):
            assert not monitor.report_write_failure(erofs("/var/lib/meshcore-bot/x"), "/var/lib/meshcore-bot/x")
        assert not (Path(tmp_path) / "meshbot_reboot.info").exists()


class TestProbe:
    def test_probe_reports_monitored_failure(self, monitor, tmp_path, monkeypatch):
        monitor.writable_prefixes = (str(tmp_path),)
        monitor.failure_threshold = 1

        def boom(self, *args, **kwargs):
            raise erofs(str(self))

        monkeypatch.setattr(Path, "write_text", boom)
        assert not monitor.probe(tmp_path / "probe.txt")
        assert monitor.consecutive_failures == 1

    def test_probe_writable_path_returns_true(self, monitor, tmp_path):
        monitor.writable_prefixes = (str(tmp_path),)
        assert monitor.probe(Path(tmp_path) / "probe.txt")
        assert monitor.consecutive_failures == 0
