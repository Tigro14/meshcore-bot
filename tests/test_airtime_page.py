"""Tests for the /airtime page.

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


class TestAirtimePage:
    def test_page_renders(self, viewer):
        resp = viewer.app.test_client().get("/airtime")
        assert resp.status_code == 200

    def test_page_says_the_airtime_is_modelled(self, viewer):
        """The page cannot present a computed figure as a measurement of the channel."""
        body = viewer.app.test_client().get("/airtime").data.decode()
        assert "estimé" in body
        assert "profil radio" in body

    def test_page_keeps_rx_and_tx_apart(self, viewer):
        """rx is a local observation, tx is what the bot decides about: never one number."""
        body = viewer.app.test_client().get("/airtime").data.decode()
        assert "RX</strong> : paquets reçus" in body
        assert "TX</strong> : demandes d’émission" in body

    def test_chart_draws_an_absent_bucket_as_a_gap(self, viewer):
        """A window with no written bucket must not render as 0.0."""
        body = viewer.app.test_client().get("/airtime").data.decode()
        assert "spanGaps: false" in body
        assert "null" in body

    def test_page_script_carries_a_csp_nonce(self, viewer):
        resp = viewer.app.test_client().get("/airtime")
        body = resp.data.decode()
        assert "<script nonce=" in body
        assert "unsafe-inline" not in resp.headers["Content-Security-Policy"].split("script-src")[0]

    def test_navbar_links_to_the_page(self, viewer):
        body = viewer.app.test_client().get("/airtime").data.decode()
        assert 'href="/airtime"' in body

    def test_disabled_state_is_shown(self, viewer):
        """Off by default, so the page has to say why there is nothing to read."""
        body = viewer.app.test_client().get("/airtime").data.decode()
        assert "[Channel_Occupancy]" in body
        assert "Recording is off" in body

    def test_the_page_keeps_the_setting_apart_from_the_snapshot(self, viewer):
        """"Recording is off now" and "these figures were written while it was on" are two
        different facts. Stating only the first presents the numbers on screen as current."""
        body = viewer.app.test_client().get("/airtime").data.decode()
        assert "was written while recording was on" in body
        assert "the setting is off now" in body

    def test_a_failed_request_retracts_the_recording_claim(self, viewer):
        """The page may only state what a response told it.

        A request that failed is not evidence that recording is off, so the claim left by the
        last successful response has to be taken back in the error path, otherwise the page
        keeps asserting a setting it no longer knows beside an error saying the request failed.
        """
        body = viewer.app.test_client().get("/airtime").data.decode()
        error_path = body.split("async loadLive()")[1].split("catch (err) {", 1)[1]
        assert "ocDisabled" in error_path
        assert "ocSnapshotNote" in error_path


class TestPassiveAirtimeAPI:
    def test_versioned_metadata_read_model_and_unknown_not_zero(self, viewer):
        response = viewer.app.test_client().get('/api/telemetry/occupancy/analysis?window=1h')
        assert response.status_code == 200
        data = response.get_json()
        assert data['schema_version'] == 1
        assert data['window_seconds'] == 3600
        assert data['local']['rx_percent'] is None
        assert data['method']['compliance'] == 'not_determined'
        assert data['repeaters'] == []
        assert data['method']['version'] == 4
        assert data['local_load']['estimated_percent'] is None
        assert data['local_load']['expected_ecl_percent'] is None
        assert data['local_load']['is_duty_cycle'] is False
        assert 'content' not in data

    def test_window_validation(self, viewer):
        response = viewer.app.test_client().get('/api/telemetry/occupancy/analysis?window=all')
        assert response.status_code == 400

    def test_existing_airtime_page_contains_both_attribution_views(self, viewer):
        body = viewer.app.test_client().get('/airtime').data.decode()
        assert 'oc-authors' in body and 'oc-repeaters' in body
        assert 'oc-analysis-window' in body

    def test_global_airtime_is_not_compared_to_a_node_duty_cycle(self, viewer):
        body = viewer.app.test_client().get('/airtime').data.decode()
        assert 'oc-analysis-reference' not in body
        assert 'Repère 10 % (par émetteur)' not in body
        assert 'Paquets distincts' in body and 'Participation' in body
        assert 'Coût radio reconstitué' in body
