"""Tests for the public /regions page and its /api/regions endpoint."""

from __future__ import annotations

import configparser
from datetime import datetime
from unittest.mock import patch

import pytest

from modules.web_viewer.app import BotDataViewer


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
        ("cors_allowed_origins", "*"), ("web_viewer_password", ""),
    ]:
        config.set("Web_Viewer", key, value)

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
    return instance


def _seed_tally(viewer, channel="#general", scoped=30, globally=10, unknown=5):
    today = datetime.now().date().isoformat()
    with viewer.db_manager.connection() as conn:
        conn.execute(
            "INSERT INTO region_scope_daily "
            "(date, channel, scoped_count, global_count, unknown_count) "
            "VALUES (?, ?, ?, ?, ?)",
            (today, channel, scoped, globally, unknown),
        )
        conn.commit()


class TestRegionsPage:
    def test_page_renders(self, viewer):
        resp = viewer.app.test_client().get("/regions")
        assert resp.status_code == 200
        assert "Avertissements de région".encode() in resp.data

    def test_page_links_to_the_admin_page(self, viewer):
        """La page publique pointe vers la page d'admin Paramètres région."""
        resp = viewer.app.test_client().get("/regions")
        body = resp.data.decode()
        assert 'href="/region-warnings"' in body
        assert "Paramètres région" in body

    def test_page_script_carries_a_csp_nonce(self, viewer):
        resp = viewer.app.test_client().get("/regions")
        body = resp.data.decode()
        assert "<script nonce=" in body
        assert "unsafe-inline" not in resp.headers["Content-Security-Policy"].split("style-src")[0]


class TestRegionsApi:
    def test_only_exposes_public_aggregates(self, viewer):
        """The public API must not leak settings, the warning message, or per-sender data."""
        _seed_tally(viewer)
        data = viewer.app.test_client().get("/api/regions").get_json()
        assert "traffic" in data
        assert "settings" not in data
        assert "default_message" not in data
        assert "message" not in data
        assert "events" not in data
        assert "budget" not in data
        assert "series" not in data

    def test_empty_when_no_traffic(self, viewer):
        data = viewer.app.test_client().get("/api/regions").get_json()
        assert data["traffic"]["channels"] == []
        assert data["traffic"]["totals"]["total"] == 0

    def test_traffic_reflects_seeded_tallies(self, viewer):
        _seed_tally(viewer)
        data = viewer.app.test_client().get("/api/regions").get_json()
        channel = data["traffic"]["channels"][0]
        assert channel["channel"] == "#general"
        assert channel["unscoped_pct"] == 25.0
        totals = data["traffic"]["totals"]
        assert totals["global"] == 10
        assert totals["scoped"] == 30
        assert totals["unknown"] == 5

    def test_window_is_clamped(self, viewer):
        data = viewer.app.test_client().get("/api/regions?days=9999").get_json()
        assert data["traffic"]["days"] == 90

    def test_bad_window_falls_back(self, viewer):
        data = viewer.app.test_client().get("/api/regions?days=abc").get_json()
        assert data["traffic"]["days"] == 14
