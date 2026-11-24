"""Tests for read-only filesystem detection and reboot signalling."""

from __future__ import annotations

import configparser
import errno
import json
import logging
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from modules.io_health import PERSISTED_STATUS_KEY, IOHealthMonitor


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

    def test_first_signal_is_not_blocked_by_the_cooldown(self, monitor, tmp_path):
        """The 0.0 reference is 'no signal yet', not a cooldown still running.

        time.monotonic() starts near zero on a freshly booted host, which is what CI
        runners are, so the first signal used to be swallowed by the cooldown check.
        """
        with patch("modules.io_health.time.monotonic", return_value=1.0):
            for _ in range(monitor.failure_threshold - 1):
                assert not monitor.report_write_failure(erofs("/var/lib/meshcore-bot/x"), "/var/lib/meshcore-bot/x")
            assert monitor.report_write_failure(erofs("/var/lib/meshcore-bot/x"), "/var/lib/meshcore-bot/x")
        assert "IOHealthWatchdog" in (Path(tmp_path) / "meshbot_reboot.info").read_text(encoding="utf-8")

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


def _monitor(config, tmp_path, mounts_path=None, db_manager=None):
    """Monitor instance pointed at the temp directory instead of the real system."""
    return IOHealthMonitor(
        config,
        logging.getLogger("test_io_health"),
        semaphore_dir=str(tmp_path),
        db_manager=db_manager,
        mounts_path=str(mounts_path) if mounts_path else str(tmp_path / "mounts"),
    )


def _mounts(tmp_path, entries):
    mounts = tmp_path / "mounts"
    # Match the canonical paths the monitor reads, including macOS /var symlinks.
    canonical = []
    for entry in entries:
        parts = entry.split()
        parts[1] = str(Path(parts[1]).resolve())
        canonical.append(' '.join(parts))
    mounts.write_text("\n".join(canonical) + "\n", encoding="utf-8")
    return mounts


class TestMountState:
    def test_mount_table_reports_read_only(self, config, tmp_path):
        mounts = _mounts(tmp_path, ["root / ext4 rw,relatime 0 0", "/dev/sata1 /var/lib/meshcore-bot ext4 ro,noatime 0 0"])
        monitor = _monitor(config, tmp_path, mounts_path=mounts)
        assert monitor.mount_state("/var/lib/meshcore-bot/data/wiki_rag/wiki_pages.jsonl") == "ro"

    def test_writable_mount_reports_rw(self, config, tmp_path):
        mounts = _mounts(tmp_path, ["/dev/sata1 /var/lib/meshcore-bot ext4 rw,noatime 0 0"])
        monitor = _monitor(config, tmp_path, mounts_path=mounts)
        assert monitor.mount_state("/var/lib/meshcore-bot/data") == "rw"

    def test_deepest_mount_wins(self, config, tmp_path):
        mounts = _mounts(tmp_path, [
            "/dev/sata1 /var/lib/meshcore-bot ext4 rw 0 0",
            "/dev/sata2 /var/lib/meshcore-bot/data ext4 ro 0 0",
        ])
        monitor = _monitor(config, tmp_path, mounts_path=mounts)
        assert monitor.mount_state("/var/lib/meshcore-bot/data/wiki_rag") == "ro"

    def test_unmounted_path_is_unknown(self, config, tmp_path):
        mounts = _mounts(tmp_path, ["root / ext4 rw 0 0"])
        monitor = _monitor(config, tmp_path, mounts_path=mounts)
        assert monitor.mount_state("/var/lib/meshcore-bot") == "unknown"

    def test_unreadable_mount_table_is_unknown(self, config, tmp_path):
        monitor = _monitor(config, tmp_path, mounts_path=tmp_path / "missing-mounts")
        assert monitor.mount_state("/var/lib/meshcore-bot") == "unknown"


class TestProbeDoesNotCountForMonitoring:
    def test_probe_status_reports_errno_without_counting(self, monitor, tmp_path, monkeypatch):
        monitor.writable_prefixes = (str(tmp_path),)

        def boom(self, *args, **kwargs):
            raise erofs(str(self))

        monkeypatch.setattr(Path, "write_text", boom)
        writable, code = monitor.probe_status(tmp_path / "probe.txt", record=False)
        assert writable is False
        assert code == "EROFS"
        assert monitor.consecutive_failures == 0

    def test_record_true_still_counts(self, monitor, tmp_path, monkeypatch):
        monitor.writable_prefixes = (str(tmp_path),)
        monitor.failure_threshold = 5

        def boom(self, *args, **kwargs):
            raise erofs(str(self))

        monkeypatch.setattr(Path, "write_text", boom)
        monitor.probe_status(tmp_path / "probe.txt", record=True)
        assert monitor.consecutive_failures == 1


class TestSemaphoreState:
    def test_no_semaphore_file_is_not_pending(self, monitor):
        assert monitor.semaphore_pending() is False

    def test_lock_held_by_another_end_is_pending(self, monitor, tmp_path):
        Path(tmp_path / "meshbot_reboot.lock").write_text("", encoding="utf-8")
        with patch("modules.io_health.fcntl.flock", side_effect=OSError(errno.EBUSY, "Device or resource busy")):
            assert monitor.semaphore_pending() is True

    def test_own_signal_makes_it_pending(self, monitor, tmp_path):
        monitor.failure_threshold = 1
        monitor.report_write_failure(erofs("/var/lib/meshcore-bot/x"), "/var/lib/meshcore-bot/x")
        assert monitor.semaphore_pending() is True

    def test_last_signal_reads_the_info_file(self, monitor, tmp_path):
        monitor.failure_threshold = 1
        monitor.report_write_failure(erofs("/var/lib/meshcore-bot/x"), "/var/lib/meshcore-bot/x")
        signal = monitor.last_signal()
        assert signal["source"] == "IOHealthWatchdog"
        assert signal["reason"].startswith("read-only filesystem")
        assert signal["timestamp"]


class TestPersistedCounters:
    def test_counters_are_persisted_and_read_back_by_another_process(self, config, tmp_path):
        stored = {}
        db = MagicMock()
        db.set_metadata = lambda key, value: stored.__setitem__(key, value)
        db.get_metadata = lambda key: stored.get(key)

        monitor = _monitor(config, tmp_path, db_manager=db)
        monitor.failure_threshold = 5
        monitor.report_write_failure(erofs("/var/lib/meshcore-bot/x"), "/var/lib/meshcore-bot/x")
        assert json.loads(stored[PERSISTED_STATUS_KEY])["failures_consecutive"] == 1

        # A fresh instance stands in for the web viewer, which is its own process.
        viewer = _monitor(config, tmp_path, db_manager=db)
        assert viewer.consecutive_failures == 0
        assert viewer.counters()["failures_consecutive"] == 1

    def test_persisted_status_survives_an_unreadable_value(self, config, tmp_path):
        db = MagicMock()
        db.get_metadata = lambda key: "not json"
        monitor = _monitor(config, tmp_path, db_manager=db)
        assert monitor.counters() == {"failures_consecutive": 0, "failures_total": 0}


class TestStatusView:
    def test_status_is_ok_and_carries_no_paths(self, config, tmp_path):
        mounts = _mounts(tmp_path, [f"{tmp_path} {tmp_path} ext4 rw 0 0"])
        monitor = _monitor(config, tmp_path, mounts_path=mounts)
        monitor.writable_prefixes = (str(tmp_path),)

        status = monitor.status()
        assert status["state"] == "ok"
        assert status["mounts_checked"] == 1
        assert status["mounts_readonly"] == 0
        assert status["probe_write_ok"] is True
        assert status["threshold_reached"] is False
        assert status["semaphore_pending"] is False
        assert status["powercycle_configured"] is False
        assert str(tmp_path) not in str(status), "aggregate view must not leak paths"
        assert not any(key in status for key in ("failure_threshold", "cooldown_seconds"))

    def test_readonly_mount_is_a_fault(self, config, tmp_path):
        mounts = _mounts(tmp_path, [f"{tmp_path} {tmp_path} ext4 ro 0 0"])
        monitor = _monitor(config, tmp_path, mounts_path=mounts)
        monitor.writable_prefixes = (str(tmp_path),)
        assert monitor.status()["state"] == "fault"
        assert monitor.status()["mounts_readonly"] == 1

    def test_probe_failure_on_a_writable_mount_is_degraded(self, config, tmp_path, monkeypatch):
        mounts = _mounts(tmp_path, [f"{tmp_path} {tmp_path} ext4 rw 0 0"])
        monitor = _monitor(config, tmp_path, mounts_path=mounts)
        monitor.writable_prefixes = (str(tmp_path),)

        def boom(self, *args, **kwargs):
            raise OSError(errno.EACCES, "Permission denied", str(self))

        monkeypatch.setattr(Path, "write_text", boom)
        status = monitor.status()
        assert status["state"] == "degraded"
        assert status["probe_errno"] == "EACCES"

    def test_disabled_monitor_is_degraded(self, config, tmp_path):
        config.set("IO_Health", "enabled", "false")
        mounts = _mounts(tmp_path, [f"{tmp_path} {tmp_path} ext4 rw 0 0"])
        monitor = _monitor(config, tmp_path, mounts_path=mounts)
        monitor.writable_prefixes = (str(tmp_path),)
        assert monitor.status()["state"] == "degraded"


class TestPowercycle:
    def test_hook_command_runs(self, config, tmp_path):
        config.set("IO_Health", "powercycle_command", "curl -fsS http://plug.local/?cmd=PowerOff")
        monitor = _monitor(config, tmp_path)

        with patch("modules.io_health.subprocess.run") as run:
            run.return_value = MagicMock(returncode=0, stdout="plug acknowledged", stderr="")
            ok, action, detail = monitor.powercycle("monitor said so")

        assert ok is True
        assert action == "command"
        assert "plug acknowledged" in detail
        assert run.call_args.args[0] == monitor.powercycle_command
        assert run.call_args.kwargs["timeout"] == monitor.powercycle_timeout_seconds

    def test_hook_failure_is_reported_as_failed(self, config, tmp_path):
        config.set("IO_Health", "powercycle_command", "false")
        monitor = _monitor(config, tmp_path)

        with patch("modules.io_health.subprocess.run") as run:
            run.return_value = MagicMock(returncode=1, stdout="", stderr="plug unreachable")
            ok, action, detail = monitor.powercycle("manual")

        assert ok is False
        assert action == "command"
        assert detail == "plug unreachable"

    def test_hook_timeout_is_reported(self, config, tmp_path):
        config.set("IO_Health", "powercycle_command", "sleep 60")
        monitor = _monitor(config, tmp_path)

        with patch("modules.io_health.subprocess.run", side_effect=subprocess.TimeoutExpired("sleep", 10)):
            ok, action, detail = monitor.powercycle("manual")

        assert ok is False
        assert "timeout" in detail

    def test_no_command_falls_back_to_the_semaphore(self, config, tmp_path):
        monitor = _monitor(config, tmp_path)
        ok, action, detail = monitor.powercycle("manual:web")

        assert ok is True
        assert action == "semaphore"
        assert detail == "raised"
        info = (Path(tmp_path) / "meshbot_reboot.info").read_text(encoding="utf-8")
        assert "manual:web" in info

    def test_already_pending_semaphore_is_not_raised_twice(self, config, tmp_path):
        monitor = _monitor(config, tmp_path)
        Path(tmp_path / "meshbot_reboot.lock").write_text("", encoding="utf-8")

        with patch("modules.io_health.fcntl.flock", side_effect=OSError(errno.EBUSY, "Device or resource busy")):
            ok, action, detail = monitor.powercycle("second request")

        assert ok is True
        assert detail == "already_pending"

    def test_cooldown_is_reported_when_it_blocks(self, config, tmp_path):
        monitor = _monitor(config, tmp_path)
        monitor._last_signal_monotonic = 5.0

        with patch("modules.io_health.time.monotonic", return_value=6.0):
            ok, action, detail = monitor.powercycle("again")

        assert ok is False
        assert detail == "cooldown"

    def test_reason_is_collapsed_to_one_line(self, config, tmp_path):
        monitor = _monitor(config, tmp_path)
        monitor.powercycle("request\nfrom\tmonitor")
        info = (Path(tmp_path) / "meshbot_reboot.info").read_text(encoding="utf-8")
        assert "request from monitor" in info
        assert len(info.splitlines()) == 3

    def test_ansi_escapes_are_stripped_from_the_reason(self, config, tmp_path):
        monitor = _monitor(config, tmp_path)
        monitor.powercycle("request \x1b[31mESC\x1b[0m")
        info = (Path(tmp_path) / "meshbot_reboot.info").read_text(encoding="utf-8")
        assert "ESC" in info
        assert "\x1b" not in info

