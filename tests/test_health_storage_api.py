"""Tests for the storage monitoring routes.

GET  /api/health/storage             — aggregate filesystem state, public read
POST /api/health/storage/powercycle  — run the power-cycle hook (admin only)

The monitoring GET is on the public allowlist because an external agent polls it,
so a password stays configured here: the read must work without a session, the
write must not.
"""

from __future__ import annotations

import configparser
import errno
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from modules.io_health import PERSISTED_STATUS_KEY, IOHealthMonitor
from modules.web_viewer import app as app_module
from modules.web_viewer.app import BotDataViewer

PASSWORD = "secret123"


@pytest.fixture
def viewer(tmp_path):
    """BotDataViewer against a temp config + empty DB (migrations create tables)."""
    config = configparser.ConfigParser()
    config.add_section("Bot")
    config.set("Bot", "db_path", str(tmp_path / "meshcore_bot.db"))
    config.set("Bot", "bot_name", "TestBot")
    config.add_section("Web_Viewer")
    for key, value in [
        ("host", "127.0.0.1"), ("port", "8080"), ("enabled", "false"),
        ("auto_start", "false"), ("debug", "false"),
        ("cors_allowed_origins", "*"), ("web_viewer_password", PASSWORD),
    ]:
        config.set("Web_Viewer", key, value)

    config.add_section("IO_Health")
    # The probe, the mount table and the semaphore are pointed at the temp directory
    # so the routes cannot touch the machine running the tests.
    config.set("IO_Health", "writable_prefixes", str(tmp_path))

    config_path = str(tmp_path / "config.ini")
    with open(config_path, "w") as handle:
        config.write(handle)

    with patch.object(BotDataViewer, "_start_database_polling"), \
         patch.object(BotDataViewer, "_start_log_tailing"), \
         patch.object(BotDataViewer, "_start_cleanup_scheduler"), \
         patch.object(BotDataViewer, "_start_dashboard_refresher"), \
         patch.object(BotDataViewer, "_setup_socketio_handlers"), \
         patch("modules.web_viewer.app.RepeaterManager"):
        instance = BotDataViewer(
            db_path=str(tmp_path / "meshcore_bot.db"), config_path=config_path)
    instance.app.testing = True

    def factory(cfg, logger, db_manager=None):
        return IOHealthMonitor(
            cfg,
            logger,
            semaphore_dir=str(tmp_path),
            db_manager=db_manager,
            mounts_path=str(tmp_path / "mounts"),
        )

    with patch.object(app_module, "IOHealthMonitor", factory):
        yield instance


@pytest.fixture
def client(viewer):
    with viewer.app.test_client() as test_client:
        yield test_client


@pytest.fixture
def auth_client(client):
    """Client with an admin session, the way the power-cycle caller would have one."""
    response = client.post("/login", data={"password": PASSWORD})
    assert response.status_code == 302
    return client


def _mounts(tmp_path: Path, entries: list[str]) -> None:
    (tmp_path / "mounts").write_text("\n".join(entries) + "\n", encoding="utf-8")


def _parse_text(body: str) -> dict[str, str]:
    """Read one ``key=value`` line per field, the way a monitoring script does."""
    fields = {}
    for line in body.splitlines():
        key, _, value = line.partition("=")
        fields[key] = value.strip("'")
    return fields


class TestStorageMonitoringGet:
    def test_returns_the_documented_keys(self, client, tmp_path):
        _mounts(tmp_path, [f"{tmp_path} {tmp_path} ext4 rw 0 0"])
        body = client.get("/api/health/storage").get_json()
        assert body is not None
        for key in (
            "state", "enabled", "mounts_checked", "mounts_readonly", "probe_write_ok",
            "probe_errno", "failures_consecutive", "failures_total", "threshold_reached",
            "semaphore_pending", "last_signal_source", "last_signal_at",
            "powercycle_configured", "uptime_seconds", "timestamp",
        ):
            assert key in body, f"missing key {key}"
        assert body["state"] == "ok"

    def test_text_format_is_one_key_value_line_per_field(self, client, tmp_path):
        _mounts(tmp_path, [f"{tmp_path} {tmp_path} ext4 rw 0 0"])
        response = client.get("/api/health/storage?format=text")
        assert response.status_code == 200
        assert response.mimetype == "text/plain"

        fields = _parse_text(response.get_data(as_text=True))
        assert fields["state"] == "ok"
        assert fields["probe_write_ok"] == "1"
        assert fields["semaphore_pending"] == "0"
        assert fields["failures_total"] == "0"

    def test_get_stays_public_while_a_password_is_configured(self, client, tmp_path):
        """An external monitoring agent polls this without a session."""
        _mounts(tmp_path, [f"{tmp_path} {tmp_path} ext4 rw 0 0"])
        assert client.get("/api/health/storage").status_code == 200

    def test_read_only_mount_is_reported_as_fault(self, client, tmp_path):
        _mounts(tmp_path, [f"{tmp_path} {tmp_path} ext4 ro,noatime 0 0"])
        body = client.get("/api/health/storage").get_json()
        assert body["state"] == "fault"
        assert body["mounts_readonly"] == 1

    def test_payload_carries_no_paths(self, client, tmp_path):
        _mounts(tmp_path, [f"{tmp_path} {tmp_path} ext4 rw 0 0"])
        for response in (
            client.get("/api/health/storage"),
            client.get("/api/health/storage?format=text"),
        ):
            assert str(tmp_path) not in response.get_data(as_text=True), "aggregate view leaked a path"

    def test_counters_are_read_from_the_shared_database(self, client, viewer, tmp_path):
        """The bot counts in its own process, so the viewer has to read them back."""
        _mounts(tmp_path, [f"{tmp_path} {tmp_path} ext4 rw 0 0"])
        viewer.db_manager.set_metadata(
            PERSISTED_STATUS_KEY,
            json.dumps({"failures_consecutive": 4, "failures_total": 9}),
        )

        body = client.get("/api/health/storage").get_json()
        assert body["failures_consecutive"] == 4
        assert body["failures_total"] == 9
        assert body["threshold_reached"] is True

    def test_a_prober_cannot_push_the_bot_over_its_threshold(self, client, tmp_path, monkeypatch):
        _mounts(tmp_path, [f"{tmp_path} {tmp_path} ext4 rw 0 0"])

        def boom(self, *args, **kwargs):
            raise OSError(errno.EACCES, "Permission denied", str(self))

        monkeypatch.setattr(Path, "write_text", boom)

        body = client.get("/api/health/storage").get_json()
        assert body["state"] == "degraded"
        assert body["probe_errno"] == "EACCES"
        assert body["failures_consecutive"] == 0


class TestPowercyclePost:
    def test_requires_admin(self, client):
        response = client.post("/api/health/storage/powercycle", json={"reason": "manual:web"})
        assert response.status_code == 401

    def test_hook_runs_after_login(self, viewer, auth_client, tmp_path):
        _mounts(tmp_path, [f"{tmp_path} {tmp_path} ext4 rw 0 0"])
        viewer.config.set("IO_Health", "powercycle_command", "curl -fsS 'http://plug.local/?cmd=PowerOff'")

        with patch("modules.io_health.subprocess.run") as run:
            run.return_value = MagicMock(returncode=0, stdout="plug acknowledged", stderr="")
            response = auth_client.post(
                "/api/health/storage/powercycle",
                json={"reason": "nvme dead"},
                headers={"X-Requested-With": "XMLHttpRequest"},
            )

        assert response.status_code == 200
        body = response.get_json()
        assert body["powercycle"] == "ok"
        assert body["action"] == "command"
        assert "plug acknowledged" in body["detail"]
        assert run.call_args.args[0] == viewer.config.get("IO_Health", "powercycle_command")

    def test_hook_failure_answers_502(self, viewer, auth_client, tmp_path):
        """A failed hook is not the same as a delivered request."""
        _mounts(tmp_path, [f"{tmp_path} {tmp_path} ext4 rw 0 0"])
        viewer.config.set("IO_Health", "powercycle_command", "false")

        with patch("modules.io_health.subprocess.run") as run:
            run.return_value = MagicMock(returncode=1, stdout="", stderr="plug unreachable")
            response = auth_client.post(
                "/api/health/storage/powercycle",
                headers={"X-Requested-With": "XMLHttpRequest"},
            )

        assert response.status_code == 502
        body = response.get_json()
        assert body["powercycle"] == "failed"
        assert body["detail"] == "plug unreachable"

    def test_text_answer_is_parseable(self, viewer, auth_client, tmp_path):
        _mounts(tmp_path, [f"{tmp_path} {tmp_path} ext4 rw 0 0"])
        viewer.config.set("IO_Health", "powercycle_command", "true")

        with patch("modules.io_health.subprocess.run") as run:
            run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
            response = auth_client.post(
                "/api/health/storage/powercycle?format=text",
                headers={"X-Requested-With": "XMLHttpRequest"},
            )

        assert response.status_code == 200
        assert response.mimetype == "text/plain"
        fields = _parse_text(response.get_data(as_text=True))
        assert fields["powercycle"] == "ok"
        assert fields["action"] == "command"

    def test_without_a_hook_the_semaphore_is_raised_for_the_watcher(self, auth_client, tmp_path):
        _mounts(tmp_path, [f"{tmp_path} {tmp_path} ext4 rw 0 0"])
        response = auth_client.post(
            "/api/health/storage/powercycle",
            json={"reason": "manual:web"},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )

        assert response.status_code == 200
        body = response.get_json()
        assert body["action"] == "semaphore"
        assert body["detail"] == "raised"
        assert body["semaphore_pending"] is True
        assert "manual:web" in (tmp_path / "meshbot_reboot.info").read_text(encoding="utf-8")

    def test_reason_is_collapsed_to_one_line(self, auth_client, tmp_path):
        _mounts(tmp_path, [f"{tmp_path} {tmp_path} ext4 rw 0 0"])
        auth_client.post(
            "/api/health/storage/powercycle",
            json={"reason": "request\nfrom\tmonitor"},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )

        info = (tmp_path / "meshbot_reboot.info").read_text(encoding="utf-8")
        assert "request from monitor" in info
        assert len(info.splitlines()) == 3
