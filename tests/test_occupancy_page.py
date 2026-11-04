"""Tests for the /occupancy page.

The page is the reader for [Channel_Occupancy]; before it existed the feature had
two API routes and no screen, so a percentage could only be read with curl. What
these tests protect is the honesty of the rendering: modelled airtime is stated as
modelled, rx is never merged with tx, and an absent bucket is a gap rather than a
zero.
"""

from __future__ import annotations

import configparser
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


class TestOccupancyPage:
    def test_page_renders(self, viewer):
        resp = viewer.app.test_client().get("/occupancy")
        assert resp.status_code == 200

    def test_page_says_the_airtime_is_modelled(self, viewer):
        """The page cannot present a computed figure as a measurement of the channel."""
        body = viewer.app.test_client().get("/occupancy").data.decode()
        assert "modelled" in body
        assert "CAD" in body

    def test_page_keeps_rx_and_tx_apart(self, viewer):
        """rx is a local observation, tx is what the bot decides about: never one number."""
        body = viewer.app.test_client().get("/occupancy").data.decode()
        assert "rx" in body and "tx" in body
        assert "the two shares are kept apart" in body.lower() or "kept apart" in body.lower()

    def test_chart_draws_an_absent_bucket_as_a_gap(self, viewer):
        """A window with no written bucket must not render as 0.0."""
        body = viewer.app.test_client().get("/occupancy").data.decode()
        assert "spanGaps: false" in body
        assert "null" in body

    def test_page_script_carries_a_csp_nonce(self, viewer):
        resp = viewer.app.test_client().get("/occupancy")
        body = resp.data.decode()
        assert "<script nonce=" in body
        assert "unsafe-inline" not in resp.headers["Content-Security-Policy"].split("script-src")[0]

    def test_navbar_links_to_the_page(self, viewer):
        body = viewer.app.test_client().get("/occupancy").data.decode()
        assert 'href="/occupancy"' in body

    def test_disabled_state_is_shown(self, viewer):
        """Off by default, so the page has to say why there is nothing to read."""
        body = viewer.app.test_client().get("/occupancy").data.decode()
        assert "[Channel_Occupancy]" in body
        assert "Recording is off" in body
