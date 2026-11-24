"""Read-only filesystem detection and shared-memory reboot signalling.

A storage fault on the target hardware shows up as ``EROFS`` once the kernel
remounts a filesystem read-only. Only writes the bot is *allowed* to perform
count: a read-only mount produced by systemd sandboxing is a configuration
fact, not a storage failure, so paths outside ``writable_prefixes`` are
ignored and can never trigger a reboot.

The reboot is not performed here. The monitor raises a semaphore in shared
memory (tmpfs, which survives a read-only disk) and an external watcher picks
it up and runs the SysRq sequence.

``powercycle()`` is the remote-trigger path: when the storage itself is dead
nothing on this host can survive long enough to reboot it, so the recovery has
to come from outside (a switched outlet). The monitor runs the configured hook,
and falls back to the semaphore when no hook is configured.
"""

from __future__ import annotations

import configparser
import errno
import fcntl
import json
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

from .security_utils import sanitize_name

DEFAULT_SEMAPHORE_DIR = "/dev/shm"
DEFAULT_WRITABLE_PREFIXES = ("/var/lib/meshcore-bot", "/var/log/meshcore-bot", "/dev/shm")
DEFAULT_MOUNTS_PATH = "/proc/mounts"
PROBE_FILENAME = ".storage_probe"
REBOOT_REASON = "IOHealthWatchdog"
PERSISTED_STATUS_KEY = "io_health.status"


class IOHealthMonitor:
    """Count read-only write failures on monitored paths and signal a reboot."""

    def __init__(
        self,
        config: configparser.ConfigParser,
        logger: logging.Logger,
        semaphore_dir: str = DEFAULT_SEMAPHORE_DIR,
        db_manager: Any = None,
        mounts_path: str = DEFAULT_MOUNTS_PATH,
    ) -> None:
        self.config = config
        self.logger = logger
        self.semaphore_dir = semaphore_dir
        # The web viewer is a separate process, so the counters are read back from
        # the shared database rather than from this instance.
        self.db_manager = db_manager
        self.mounts_path = mounts_path
        self.enabled = self._bool("enabled", True)
        self.reboot_on_erofs = self._bool("reboot_on_erofs", False)
        self.failure_threshold = max(1, self._int("failure_threshold", 3))
        self.cooldown_seconds = max(0, self._int("cooldown_seconds", 900))
        self.powercycle_command = self._get("powercycle_command", "").strip()
        self.powercycle_timeout_seconds = max(1, self._int("powercycle_timeout_seconds", 10))
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
        self._persist_counters()
        if self.consecutive_failures < self.failure_threshold:
            return False

        return self.signal_reboot(f"read-only filesystem on {path}")

    def _persist_counters(self) -> None:
        """Store the counters for the web viewer, which runs in another process.

        Guarded: a storage fault is exactly the situation where this write fails, and
        the counters are not what the recovery path depends on.
        """
        if self.db_manager is None:
            return
        try:
            self.db_manager.set_metadata(
                PERSISTED_STATUS_KEY,
                json.dumps({
                    "failures_consecutive": self.consecutive_failures,
                    "failures_total": self.total_failures,
                }),
            )
        except Exception as exc:
            self.logger.warning("Could not persist IO_Health counters: %s", exc)

    def counters(self) -> dict[str, int]:
        """Counters from the shared database when available, else this instance's."""
        if self.db_manager is not None:
            try:
                stored = self.db_manager.get_metadata(PERSISTED_STATUS_KEY)
                data = json.loads(stored) if stored else None
            except Exception as exc:
                self.logger.warning("Could not read persisted IO_Health counters: %s", exc)
                data = None
            if isinstance(data, dict):
                return {
                    "failures_consecutive": int(data.get("failures_consecutive", self.consecutive_failures)),
                    "failures_total": int(data.get("failures_total", self.total_failures)),
                }
        return {
            "failures_consecutive": self.consecutive_failures,
            "failures_total": self.total_failures,
        }

    def mount_state(self, path: Any) -> str:
        """Return 'ro', 'rw', or 'unknown' for the filesystem that mounts ``path``.

        Read from the kernel mount table rather than from a previous write error, so a
        monitoring probe reports the state the kernel actually has right now.
        """
        resolved = os.path.realpath(str(path))
        try:
            lines = Path(self.mounts_path).read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            self.logger.warning("Could not read the mount table %s: %s", self.mounts_path, exc)
            return "unknown"

        best_options: Optional[str] = None
        best_length = -1
        for line in lines:
            parts = line.split()
            # mount table line: device mountpoint fstype options dump pass
            if len(parts) < 4:
                continue
            mount_point = os.path.normpath(parts[1])
            if resolved == mount_point or resolved.startswith(mount_point + os.sep):
                if len(mount_point) > best_length:
                    best_options, best_length = parts[3], len(mount_point)

        if best_options is None:
            return "unknown"
        return "ro" if "ro" in best_options.split(",") else "rw"

    def probe_status(self, path: Any, record: bool = True) -> tuple[bool, Optional[str]]:
        """Attempt a small write and return ``(writable, errno_name)``.

        ``record=False`` does not count the failure: a monitoring agent polling the
        web route must not be able to push the bot over its reboot threshold.
        """
        try:
            target = Path(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("", encoding="utf-8")
            return True, None
        except OSError as exc:
            if record:
                self.report_write_failure(exc, path)
            number = getattr(exc, "errno", None)
            return False, errno.errorcode.get(number, "UNKNOWN") if number is not None else "UNKNOWN"

    def probe(self, path: Any, record: bool = True) -> bool:
        """Attempt a small write on a monitored path; returns True when writable."""
        return self.probe_status(path, record=record)[0]

    def semaphore_pending(self) -> bool:
        """True when a reboot request is outstanding (the semaphore lock is held)."""
        if self._lock_fd is not None:
            return True
        lock_path = os.path.join(self.semaphore_dir, "meshbot_reboot.lock")
        try:
            fd = os.open(lock_path, os.O_RDONLY)
        except OSError:
            return False  # no semaphore file yet
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return False
        except OSError:
            return True  # held by this process or by the watcher's other end
        finally:
            os.close(fd)

    def last_signal(self) -> dict[str, str]:
        """The info file written when the semaphore was raised, as ``source``/``reason``/``timestamp``."""
        info_path = os.path.join(self.semaphore_dir, "meshbot_reboot.info")
        try:
            text = Path(info_path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return {}

        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines:
            return {}
        fields: dict[str, str] = {"source": lines[0]}
        for line in lines[1:]:
            key, _, value = line.partition(":")
            if key and value:
                fields[key.strip()] = value.strip()
        return fields

    def status(self) -> dict[str, Any]:
        """Storage state for an external monitor: aggregate counters, no paths, no config values."""
        prefixes = self.writable_prefixes
        readonly = [prefix for prefix in prefixes if self.mount_state(prefix) == "ro"]
        # A directory cannot be written to, so the probe is a file inside each prefix.
        codes = sorted({
            code
            for prefix in prefixes
            for code in [self.probe_status(os.path.join(prefix, PROBE_FILENAME), record=False)[1]]
            if code
        })
        counters = self.counters()
        signal = self.last_signal()

        if readonly or "EROFS" in codes:
            state = "fault"
        elif codes or not self.enabled:
            state = "degraded"
        else:
            state = "ok"

        return {
            "state": state,
            "enabled": self.enabled,
            "mounts_checked": len(prefixes),
            "mounts_readonly": len(readonly),
            "probe_write_ok": not codes,
            "probe_errno": ",".join(codes),
            "failures_consecutive": counters["failures_consecutive"],
            "failures_total": counters["failures_total"],
            "threshold_reached": counters["failures_consecutive"] >= self.failure_threshold,
            "semaphore_pending": self.semaphore_pending(),
            "last_signal_source": signal.get("source", ""),
            "last_signal_at": signal.get("timestamp", ""),
            "powercycle_configured": bool(self.powercycle_command),
        }

    def powercycle(self, reason: str = "manual") -> tuple[bool, str, str]:
        """Trigger the power cycle through the configured hook, or raise the semaphore.

        Returns ``(ok, action, detail)``. With no hook configured there is nothing this
        host can do to recover a dead disk, so the shared-memory semaphore is raised for
        the external watcher instead.
        """
        # One line and no control characters or ANSI escapes: the reason reaches both
        # the log and the semaphore info file.
        reason = sanitize_name(" ".join(str(reason).split()), max_length=120) or "manual"

        if not self.powercycle_command:
            pending_before = self.semaphore_pending()
            raised = self.signal_reboot(reason)
            if raised:
                detail = "already_pending" if pending_before else "raised"
            elif self.cooldown_seconds and self._last_signal_monotonic:
                detail = "cooldown"
            else:
                detail = "error"
            return raised, "semaphore", detail

        try:
            result = subprocess.run(
                self.powercycle_command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=self.powercycle_timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            self.logger.error(
                "Power cycle hook timed out after %d s: %s", self.powercycle_timeout_seconds, reason
            )
            return False, "command", f"timeout after {self.powercycle_timeout_seconds} s"
        except OSError as exc:
            self.logger.error("Power cycle hook could not run: %s", exc)
            return False, "command", str(exc)

        ok = result.returncode == 0
        detail = (result.stdout or result.stderr or "").strip()[:200] or f"exit {result.returncode}"
        self.logger.warning("Power cycle hook %s (%s): %s", "ok" if ok else "failed", reason, detail)
        return ok, "command", detail

    def signal_reboot(self, reason: str) -> bool:
        """Raise the shared-memory reboot semaphore; the watcher performs the reboot."""
        if self._lock_fd is not None:
            return True
        now = time.monotonic()
        # 0.0 means "no signal yet". time.monotonic() starts near zero on a freshly
        # booted host (containers, CI runners), so comparing now against an unset
        # reference made the very first signal look like it was inside the cooldown.
        if (
            self.cooldown_seconds
            and self._last_signal_monotonic
            and (now - self._last_signal_monotonic) < self.cooldown_seconds
        ):
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
        counters = self.counters()
        return {
            "enabled": self.enabled,
            "reboot_on_erofs": self.reboot_on_erofs,
            "failure_threshold": self.failure_threshold,
            "cooldown_seconds": self.cooldown_seconds,
            "writable_prefixes": list(self.writable_prefixes),
            "consecutive_failures": counters["failures_consecutive"],
            "total_failures": counters["failures_total"],
            # Held by this process or by another one: the semaphore is shared memory.
            "reboot_signalled": self.semaphore_pending(),
            "powercycle_configured": bool(self.powercycle_command),
        }
