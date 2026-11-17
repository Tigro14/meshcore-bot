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
    def test_main_page_prioritizes_one_curve_and_short_lists(self, viewer):
        response = viewer.app.test_client().get('/airtime')
        assert response.status_code == 200
        body = response.data.decode()
        main, details = body.split('<div class="modal fade" id="airtime-details"', 1)
        assert main.count('<canvas') == 1
        assert '<table' not in main
        assert 'Occupation moyenne du canal' in main
        assert 'Paquets reçus par minute' in main
        assert 'Duty cycle estimé' in main
        assert 'airtime-duty-list' in main and 'airtime-source-list' in main
        assert details.count('<table') == 3
        assert 'airtime-search' in details
        assert 'Profil radio' in body

    def test_duty_reference_is_per_repeater_and_hour(self, viewer):
        body = viewer.app.test_client().get('/airtime').data.decode()
        assert '10 % = 6 min/h par répéteur' in body
        assert 'Duty cycle — dernière heure' in body
        assert 'Estimations partielles' in body
        assert 'Repère 10 % (par émetteur)' not in body

    def test_chart_draws_an_absent_bucket_as_a_gap(self, viewer):
        body = viewer.app.test_client().get('/airtime').data.decode()
        assert "type: 'bar'" in body
        assert 'airtimeSilence' in body
        assert '0 % : silence confirmé.' in body
        assert 'Écoute locale de Baliz' not in body
        assert 'Tirets gris : données indisponibles' not in body
        assert 'null' in body

    def test_page_script_carries_a_csp_nonce(self, viewer):
        response = viewer.app.test_client().get('/airtime')
        assert '<script nonce=' in response.data.decode()
        assert 'unsafe-inline' not in response.headers['Content-Security-Policy'].split('script-src')[0]

    def test_navbar_links_to_the_page(self, viewer):
        assert 'href="/airtime"' in viewer.app.test_client().get('/airtime').data.decode()

    def test_recording_off_is_distinguished_from_old_data_and_request_failure(self, viewer):
        body = viewer.app.test_client().get('/airtime').data.decode()
        assert 'Enregistrement désactivé. Les données affichées sont historiques.' in body
        assert 'État de l’enregistrement indisponible.' in body
        assert 'Les données précédentes ne sont pas à jour.' in body


class TestPassiveAirtimeAPI:
    def test_versioned_read_model_keeps_unknown_separate_from_zero(self, viewer):
        response = viewer.app.test_client().get('/api/telemetry/occupancy/analysis?window=1h')
        assert response.status_code == 200
        data = response.get_json()
        assert data['schema_version'] == 1
        assert data['window_seconds'] == 3600
        assert data['local']['rx_percent'] is None
        assert data['local']['rx_packets_per_minute'] is None
        assert data['local']['rx_mean_frame_bytes'] is None
        assert data['local']['rx_frame_bytes'] == 0
        assert data['local']['rx_last_five_minutes']['window_seconds'] == 300
        assert data['local']['rx_last_five_minutes']['rx_percent'] is None
        assert data['local']['rx_last_five_minutes']['rx_packets_per_minute'] is None
        assert data['local']['rx_peak_five_minute_percent'] is None
        assert len(data['local']['rx_series']) == 60
        assert all(bucket['percent'] is None for bucket in data['local']['rx_series'])
        assert data['method']['compliance'] == 'not_determined'
        assert data['repeaters'] == []
        assert data['method']['version'] == 6
        assert data['forwarding_model']['status'] == 'insufficient_training'
        assert data['forwarding_model']['is_fdr'] is False
        assert data['local_load']['estimated_percent'] is None
        assert data['local_load']['expected_ecl_percent'] is None
        assert data['local_load']['is_duty_cycle'] is False
        assert 'content' not in data

    def test_window_validation(self, viewer):
        assert viewer.app.test_client().get('/api/telemetry/occupancy/analysis?window=all').status_code == 400
