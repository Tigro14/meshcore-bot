"""Read-only filesystem detection and shared-memory reboot signalling.

A storage fault on the target hardware shows up as ``EROFS`` once the kernel
remounts a filesystem read-only. Only writes the bot is *allowed* to perform
count: a read-only mount produced by systemd sandboxing is a configuration
fact, not a storage failure, so paths outside ``writable_prefixes`` are
ignored and can never trigger a reboot.

The reboot is not performed here. The monitor raises a semaphore in shared
memory (tmpfs, which survives a read-only disk) and an external watcher picks
it up and runs the SysRq sequence.
"""

from __future__ import annotations

import configparser
import errno
import fcntl
import logging
import os
import time
from pathlib import Path
from typing import Any

DEFAULT_SEMAPHORE_DIR = "/dev/shm"
DEFAULT_WRITABLE_PREFIXES = ("/var/lib/meshcore-bot", "/var/log/meshcore-bot", "/dev/shm")
REBOOT_REASON = "IOHealthWatchdog"


class IOHealthMonitor:
    """Count read-only write failures on monitored paths and signal a reboot."""

    def __init__(
        self,
        config: configparser.ConfigParser,
        logger: logging.Logger,
        semaphore_dir: str = DEFAULT_SEMAPHORE_DIR,
    ) -> None:
        self.config = config
        self.logger = logger
        self.semaphore_dir = semaphore_dir
        self.enabled = self._bool("enabled", True)
        self.reboot_on_erofs = self._bool("reboot_on_erofs", True)
        self.failure_threshold = max(1, self._int("failure_threshold", 3))
        self.cooldown_seconds = max(0, self._int("cooldown_seconds", 900))
        self.writable_prefixes = tuple(
            item.strip()
            for item in self._get("writable_prefixes", ",".join(DEFAULT_WRITABLE_PREFIXES)).split(",")
            if item.strip()
        )
        self.consecutive_failures = 0
        self.total_failures = 0
        self._last_signal_monotonic = 0.0
        self._lock_fd: int | None = None

    def _get(self, key: str, fallback: str) -> str:
        return self.config.get("IO_Health", key, fallback=fallback)

    def _bool(self, key: str, fallback: bool) -> bool:
        return self.config.getboolean("IO_Health", key, fallback=fallback)

    def _int(self, key: str, fallback: int) -> int:
        return self.config.getint("IO_Health", key, fallback=fallback)

    def is_monitored_path(self, path: Any) -> bool:
        """True when the path belongs to a location the bot is expected to write to."""
        resolved = os.path.normpath(str(path))
        for prefix in self.writable_prefixes:
            normalized = os.path.normpath(prefix)
            if resolved == normalized or resolved.startswith(normalized + os.sep):
                return True
        return False

    def report_write_failure(self, exc: OSError, path: Any) -> bool:
        """Record a write failure; returns True when the reboot semaphore was raised."""
        if not self.enabled or not self.reboot_on_erofs:
            return False
        if getattr(exc, "errno", None) != errno.EROFS:
            return False

        self.total_failures += 1
        if not self.is_monitored_path(path):
            self.logger.warning(
                "Read-only filesystem on %s is outside the monitored prefixes; not a storage fault",
                path,
            )
            return False

        self.consecutive_failures += 1
        self.logger.error(
            "Read-only filesystem write failure on %s (%d/%d)",
            path,
            self.consecutive_failures,
            self.failure_threshold,
        )
        if self.consecutive_failures < self.failure_threshold:
            return False

        return self.signal_reboot(f"read-only filesystem on {path}")

    def probe(self, path: Any) -> bool:
        """Attempt a small write on a monitored path; returns True when writable."""
        try:
            target = Path(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("", encoding="utf-8")
        except OSError as exc:
            self.report_write_failure(exc, path)
            return False
        return True

    def signal_reboot(self, reason: str) -> bool:
        """Raise the shared-memory reboot semaphore; the watcher performs the reboot."""
        if self._lock_fd is not None:
            return True
        now = time.monotonic()
        if self.cooldown_seconds and (now - self._last_signal_monotonic) < self.cooldown_seconds:
            self.logger.warning(
                "Reboot not signalled: %d s cooldown still running (last signal %.0f s ago)",
                self.cooldown_seconds,
                now - self._last_signal_monotonic,
            )
            return False

        lock_path = os.path.join(self.semaphore_dir, "meshbot_reboot.lock")
        info_path = os.path.join(self.semaphore_dir, "meshbot_reboot.info")
        try:
            os.makedirs(self.semaphore_dir, exist_ok=True)
            fd = os.open(lock_path, os.O_CREAT | os.O_WRONLY, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(fd)
                self.logger.warning("Reboot semaphore already held; reboot is already pending")
                return True

            self._lock_fd = fd
            self._last_signal_monotonic = now
            try:
                with open(info_path, "w", encoding="utf-8") as handle:
                    handle.write(f"{REBOOT_REASON}\nreason: {reason}\ntimestamp: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
            except OSError as exc:
                self.logger.warning("Could not write reboot info file %s: %s", info_path, exc)

            self.logger.error("Reboot signalled through the shared-memory semaphore: %s", reason)
            return True
        except OSError as exc:
            self.logger.error("Could not raise the reboot semaphore %s: %s", lock_path, exc)
            return False

    def stats(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "reboot_on_erofs": self.reboot_on_erofs,
            "failure_threshold": self.failure_threshold,
            "cooldown_seconds": self.cooldown_seconds,
            "writable_prefixes": list(self.writable_prefixes),
            "consecutive_failures": self.consecutive_failures,
            "total_failures": self.total_failures,
            "reboot_signalled": self._lock_fd is not None,
        }
