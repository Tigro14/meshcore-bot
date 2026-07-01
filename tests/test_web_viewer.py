#!/usr/bin/env python3
"""Tests for modules/web_viewer/app.py — BotDataViewer Flask routes and API endpoints.

Uses Flask's built-in test client.  Background threads (database polling, log
tailing, cleanup scheduler) are patched to no-ops so the fixture is fast and
side-effect free.
"""

import configparser
import json
import logging
import sqlite3
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import pytest

from modules.web_viewer.app import BotDataViewer

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_config(path: Path, db_path: str) -> None:
    cfg = configparser.ConfigParser()
    cfg["Connection"] = {"connection_type": "serial", "serial_port": "/dev/ttyUSB0"}
    cfg["Bot"] = {"bot_name": "TestBot", "db_path": db_path, "prefix_bytes": "1"}
    cfg["Channels"] = {"monitor_channels": "general"}
    cfg["Path_Command"] = {
        "graph_capture_enabled": "false",
        "graph_write_strategy": "immediate",
    }
    with open(path, "w") as f:
        cfg.write(f)


def _fake_setup_logging(self: BotDataViewer) -> None:
    """Replace file-based logging with an in-memory logger for tests."""
    self.logger = logging.getLogger("test_web_viewer")
    self.logger.setLevel(logging.DEBUG)
    if not self.logger.handlers:
        self.logger.addHandler(logging.NullHandler())
    self.logger.propagate = False


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def viewer(tmp_path_factory):
    """Create a BotDataViewer with a real temp SQLite DB and Flask test client.

    Background threads are suppressed.  The fixture is module-scoped so the
    expensive DB initialisation only runs once per test module.
    """
    tmp = tmp_path_factory.mktemp("web_viewer")
    db_path = str(tmp / "test.db")
    config_path = str(tmp / "config.ini")
    _write_config(Path(config_path), db_path)

    with (
        patch.object(BotDataViewer, "_setup_logging", _fake_setup_logging),
        patch.object(BotDataViewer, "_start_database_polling", lambda self: None),
        patch.object(BotDataViewer, "_start_log_tailing", lambda self: None),
        patch.object(BotDataViewer, "_start_cleanup_scheduler", lambda self: None),
    ):
        v = BotDataViewer(db_path=db_path, config_path=config_path)

    v.app.config["TESTING"] = True
    v.app.config["WTF_CSRF_ENABLED"] = False
    yield v


@pytest.fixture
def client(viewer):
    """Flask test client with an application context."""
    with viewer.app.test_client() as c:
        yield c


@pytest.fixture
def auth_viewer(tmp_path_factory):
    """BotDataViewer with password authentication enabled."""
    tmp = tmp_path_factory.mktemp("web_viewer_auth")
    db_path = str(tmp / "test.db")
    config_path = str(tmp / "config.ini")

    cfg = configparser.ConfigParser()
    cfg["Connection"] = {"connection_type": "serial", "serial_port": "/dev/ttyUSB0"}
    cfg["Bot"] = {"bot_name": "TestBot", "db_path": db_path, "prefix_bytes": "1"}
    cfg["Web_Viewer"] = {"web_viewer_password": "secret123"}
    cfg["Path_Command"] = {
        "graph_capture_enabled": "false",
        "graph_write_strategy": "immediate",
    }
    with open(config_path, "w") as f:
        cfg.write(f)

    with (
        patch.object(BotDataViewer, "_setup_logging", _fake_setup_logging),
        patch.object(BotDataViewer, "_start_database_polling", lambda self: None),
        patch.object(BotDataViewer, "_start_log_tailing", lambda self: None),
        patch.object(BotDataViewer, "_start_cleanup_scheduler", lambda self: None),
    ):
        v = BotDataViewer(db_path=db_path, config_path=config_path)

    v.app.config["TESTING"] = True
    yield v


@pytest.fixture
def auth_client(auth_viewer):
    with auth_viewer.app.test_client() as c:
        yield c


@pytest.fixture(autouse=True)
def cleanup_sqlite_connections(monkeypatch):
    """Track and close SQLite connections opened during each test."""
    tracked_connections = []
    original_connect = sqlite3.connect

    def _tracked_connect(*args, **kwargs):
        conn = original_connect(*args, **kwargs)
        tracked_connections.append(conn)
        return conn

    monkeypatch.setattr(sqlite3, "connect", _tracked_connect)
    yield
    for conn in tracked_connections:
        try:
            conn.close()
        except sqlite3.Error:
            pass


# ---------------------------------------------------------------------------
# Helper: insert a contact row so contact-related routes have data
# ---------------------------------------------------------------------------

def _insert_contact(viewer: BotDataViewer, public_key: str = "aabbccdd" * 8,
                    name: str = "TestNode") -> str:
    with closing(sqlite3.connect(viewer.db_path)) as conn:
        conn.execute(
            """INSERT OR IGNORE INTO complete_contact_tracking
               (public_key, name, role, device_type, is_starred, is_currently_tracked)
               VALUES (?, ?, 'companion', 'device', 0, 1)""",
            (public_key, name),
        )
        conn.commit()
    return public_key


# ===========================================================================
# Page routes (HTML)
# ===========================================================================

class TestPageRoutes:

    def test_index(self, client):
        resp = client.get("/")
        assert resp.status_code == 200

    def test_index_live_activity_controls(self, client):
        """Dashboard index page contains scroll buttons and type-filter checkboxes."""
        resp = client.get("/")
        assert resp.status_code == 200
        html = resp.data.decode()
        # Scroll buttons
        assert 'id="live-scroll-top"' in html
        assert 'id="live-scroll-bottom"' in html
        assert 'scrollLiveFeed' in html
        # Filter checkboxes with data-type attributes
        assert 'data-type="packet"' in html
        assert 'data-type="command"' in html
        assert 'data-type="message"' in html
        assert 'live-filter-cb' in html
        # [#channel] prefix logic present in JS
        assert 'applyFilters' in html

    def test_realtime(self, client):
        resp = client.get("/realtime")
        assert resp.status_code == 200

    def test_realtime_scroll_controls(self, client):
        """Realtime page has scroll buttons, type filters, and channel labels in messages."""
        resp = client.get("/realtime")
        assert resp.status_code == 200
        html = resp.data.decode()
        # Scroll buttons present for all three streams
        assert 'id="cmd-scroll-top"' in html
        assert 'id="cmd-scroll-bottom"' in html
        assert 'id="pkt-scroll-top"' in html
        assert 'id="pkt-scroll-bottom"' in html
        assert 'id="msg-scroll-top"' in html
        assert 'id="msg-scroll-bottom"' in html
        # scrollStream JS function present
        assert 'scrollStream' in html
        # Type filter checkboxes for each stream panel
        assert 'rt-filter-cb' in html
        assert 'id="rt-filter-command"' in html
        assert 'id="rt-filter-packet"' in html
        assert 'id="rt-filter-message"' in html
        assert 'id="command-card"' in html
        assert 'id="packet-card"' in html
        assert 'id="message-card"' in html
        # Live message helpers: strip duplicate bracket tags, per-channel accent
        assert 'stripLeadingChannelBracketTag' in html
        assert 'channelAccentStyles' in html

    def test_logs(self, client):
        resp = client.get("/logs")
        assert resp.status_code == 200

    def test_contacts(self, client):
        resp = client.get("/contacts")
        assert resp.status_code == 200

    def test_cache(self, client):
        resp = client.get("/cache")
        assert resp.status_code == 302
        assert resp.headers["Location"].endswith("/config#database")

    def test_stats(self, client):
        resp = client.get("/stats")
        assert resp.status_code == 200

    def test_greeter(self, client):
        resp = client.get("/greeter")
        assert resp.status_code == 200

    def test_feeds(self, client):
        resp = client.get("/feeds")
        assert resp.status_code == 200

    def test_radio(self, client):
        resp = client.get("/radio")
        assert resp.status_code == 200

    def test_config(self, client):
        resp = client.get("/config")
        assert resp.status_code == 200

    def test_infos(self, client):
        resp = client.get("/infos")
        assert resp.status_code == 200

    def test_infos_contains_nav_link(self, client):
        """The /infos page renders and the navbar contains the Infos link."""
        resp = client.get("/infos")
        assert resp.status_code == 200
        html = resp.data.decode()
        assert 'href="/infos"' in html

    def test_infos_shows_bot_config(self, client):
        """The /infos page shows at least the channels and DM configuration."""
        resp = client.get("/infos")
        assert resp.status_code == 200
        html = resp.data.decode()
        # Config section should be rendered
        assert "Monitored Channels" in html
        assert "Direct Messages" in html

    def test_infos_uses_meshcore_io_url(self, client):
        """The /infos page links to meshcore.io (not the old co.uk domain)."""
        resp = client.get("/infos")
        assert resp.status_code == 200
        html = resp.data.decode()
        assert 'href="https://meshcore.io"' in html
        assert "meshcore.io" not in html
        html = resp.data.decode()
        assert 'href="https://meshcore.io"' in html
        assert "meshcore.io" not in html

    def test_base_footer_uses_meshcore_io_url(self, client):
        """The base footer should link to meshcore.io on all pages."""
        resp = client.get("/")
        assert resp.status_code == 200
        html = resp.data.decode()
        assert 'href="https://meshcore.io"' in html
        assert "meshcore.io" not in html

    def test_infos_disabled_command_filtered(self, tmp_path_factory):
        """A command disabled in config must not appear on the /infos page."""
        from unittest.mock import patch  # noqa: PLC0415

        tmp = tmp_path_factory.mktemp("infos_disabled")
        db_path = str(tmp / "test.db")
        config_path = str(tmp / "config.ini")

        cfg = configparser.ConfigParser()
        cfg["Connection"] = {"connection_type": "serial", "serial_port": "/dev/ttyUSB0"}
        cfg["Bot"] = {"bot_name": "TestBot", "db_path": db_path, "prefix_bytes": "1"}
        cfg["Channels"] = {"monitor_channels": "general"}
        cfg["Path_Command"] = {"graph_capture_enabled": "false", "graph_write_strategy": "immediate"}
        # Disable the ping command
        cfg["Ping_Command"] = {"enabled": "false"}
        with open(config_path, "w") as f:
            cfg.write(f)

        with (
            patch.object(BotDataViewer, "_setup_logging", _fake_setup_logging),
            patch.object(BotDataViewer, "_start_database_polling", lambda self: None),
            patch.object(BotDataViewer, "_start_log_tailing", lambda self: None),
            patch.object(BotDataViewer, "_start_cleanup_scheduler", lambda self: None),
        ):
            v = BotDataViewer(db_path=db_path, config_path=config_path)

        v.app.config["TESTING"] = True

        # Verify at the data layer: _get_command_info() must not include 'ping'
        command_names = [cmd['name'] for cmd in v._get_command_info()]
        assert 'ping' not in command_names

        # Verify at the page layer: the rendered HTML must not contain the ping entry
        with v.app.test_client() as c:
            resp = c.get("/infos")
        assert resp.status_code == 200
        html = resp.data.decode()
        # "ping" command should not appear in the command table when disabled
        # We check there is no badge with 'ping' as a trigger keyword
        # The command name cell uses <strong>ping</strong>
        assert "<strong>ping</strong>" not in html

    def test_mesh(self, client):
        resp = client.get("/mesh")
        assert resp.status_code == 200


# ===========================================================================
# Health routes
# ===========================================================================

class TestHealthRoutes:

    def test_api_health_status(self, client):
        resp = client.get("/api/health")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "healthy"
        assert "connected_clients" in data
        assert "timestamp" in data
        assert data["version"] == "modern_2.0"

    def test_api_health_client_count(self, client):
        resp = client.get("/api/health")
        data = resp.get_json()
        assert isinstance(data["connected_clients"], int)
        assert data["connected_clients"] >= 0

    def test_api_system_health_returns_json(self, client):
        resp = client.get("/api/system-health")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "status" in data or "error" in data


# ===========================================================================
# Radio routes
# ===========================================================================

class TestRadioRoutes:

    def test_radio_status_returns_json(self, client):
        resp = client.get("/api/radio/status")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "status_known" in data

    def test_radio_status_unknown_when_no_metadata(self, client, viewer):
        # Ensure key is absent
        viewer.db_manager.set_metadata("radio_connected", None) if hasattr(
            viewer.db_manager, "set_metadata"
        ) else None
        resp = client.get("/api/radio/status")
        assert resp.status_code == 200

    def test_radio_reboot_queues_operation(self, client):
        resp = client.post("/api/radio/reboot")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert "operation_id" in data

    def test_radio_connect_action(self, client):
        resp = client.post(
            "/api/radio/connect",
            json={"action": "connect"},
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True

    def test_radio_disconnect_action(self, client):
        resp = client.post(
            "/api/radio/connect",
            json={"action": "disconnect"},
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True

    def test_radio_connect_invalid_action(self, client):
        resp = client.post(
            "/api/radio/connect",
            json={"action": "explode"},
            content_type="application/json",
        )
        assert resp.status_code == 400
        data = resp.get_json()
        assert "error" in data

    def test_radio_connect_missing_action(self, client):
        resp = client.post(
            "/api/radio/connect",
            json={},
            content_type="application/json",
        )
        assert resp.status_code == 400


# ===========================================================================
# Contact routes
# ===========================================================================

class TestContactRoutes:

    def test_api_contacts_default(self, client):
        resp = client.get("/api/contacts")
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, dict)

    def test_api_contacts_since_7d(self, client):
        resp = client.get("/api/contacts?since=7d")
        assert resp.status_code == 200

    def test_api_contacts_since_all(self, client):
        resp = client.get("/api/contacts?since=all")
        assert resp.status_code == 200

    def test_api_contacts_invalid_since_uses_default(self, client):
        resp = client.get("/api/contacts?since=forever")
        assert resp.status_code == 200

    def test_toggle_star_missing_public_key(self, client):
        resp = client.post(
            "/api/toggle-star-contact",
            json={},
            content_type="application/json",
        )
        assert resp.status_code == 400
        data = resp.get_json()
        assert "error" in data

    def test_toggle_star_unknown_contact(self, client):
        resp = client.post(
            "/api/toggle-star-contact",
            json={"public_key": "0" * 64},
            content_type="application/json",
        )
        assert resp.status_code == 404

    def test_toggle_star_known_contact(self, client, viewer):
        pk = _insert_contact(viewer, "1122334455667788" * 4, "StarNode")
        resp = client.post(
            "/api/toggle-star-contact",
            json={"public_key": pk},
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert "is_starred" in data

    def test_toggle_star_toggles_value(self, client, viewer):
        pk = _insert_contact(viewer, "aabbccdd11223344" * 4, "ToggleNode")
        # First call: star
        r1 = client.post("/api/toggle-star-contact", json={"public_key": pk},
                         content_type="application/json")
        starred = r1.get_json()["is_starred"]
        # Second call: unstar
        r2 = client.post("/api/toggle-star-contact", json={"public_key": pk},
                         content_type="application/json")
        unstarred = r2.get_json()["is_starred"]
        assert starred != unstarred

    def test_purge_preview_returns_json(self, client):
        resp = client.get("/api/contacts/purge-preview?days=30")
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, (dict, list))

    def test_purge_contacts_post(self, client):
        resp = client.post(
            "/api/contacts/purge",
            json={"days": 365},
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert "purged_count" in data
