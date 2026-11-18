"""Tests for the /airtime page.

The page reads two read models that must never be mixed: the passive analysis
([airtime_analysis]) drives the curve, the duty estimates and the modal detail
tables, while the three by-airtime ledgers ([Channel_Occupancy]) drive the
"Registres par airtime" card. What these tests protect is the honesty of the
rendering: modelled airtime is stated as modelled, an unknown is N/D rather
than a zero, the three registers are never summed, and every period states the
span it actually answers for.
"""

from __future__ import annotations

import configparser
import re
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
        # The one visible table is the ledger card: everything else that
        # ranks stations lives in the modal, over the analysis read model.
        assert main.count('<table') == 1
        assert 'Occupation moyenne du canal' in main
        assert 'Paquets reçus par minute' in main
        assert 'Duty cycle estimé' in main
        assert 'airtime-duty-list' in main and 'airtime-source-list' in main
        assert 'Registres par airtime' in main
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

    def test_the_ledger_card_states_modelled_airtime_and_never_sums_registers(self, viewer):
        """The adopted design may not quietly drop the page's honesty contract."""
        body = viewer.app.test_client().get('/airtime').data.decode()
        card = body.split('id="airtime-ledger-title"', 1)[1].split('</section>', 1)[0]
        assert 'L’airtime est modélisé, pas mesuré.' in card
        assert 'ne sont jamais additionnés' in card
        assert 'N/D : valeur inconnue, jamais une mesure à zéro.' in card

    def test_the_ledger_card_switches_the_three_registers(self, viewer):
        body = viewer.app.test_client().get('/airtime').data.decode()
        assert 'airtime-ledger-role' in body
        assert '<option value="authors">Auteurs des paquets</option>' in body
        assert '<option value="entry_relays">Premiers relais des chemins</option>' in body
        assert '<option value="emitters">Émetteurs entendus</option>' in body
        # One fetch per period: the selector switches registers inside the page.
        assert "/api/telemetry/occupancy?limit=10&days=" in body
        assert "top_senders" in body and "top_emitters" in body and "top_relays" in body
        assert "period_totals?.senders" in body
        assert "period_totals?.relays" in body
        assert "period_totals?.emitters" in body

    def test_each_ledger_table_leads_with_its_whole_period_recap(self, viewer):
        body = viewer.app.test_client().get('/airtime').data.decode()
        assert 'airtime-totals-row' in body
        assert '<strong>Tout le registre</strong>' in body
        assert 'entrées' in body and 'listées' in body
        assert 'if (!totals || totals.airtime_ms == null) return' in body

    def test_page_states_the_span_the_ledgers_answer_for(self, viewer):
        """The selector says "Dernières 24 heures" while the ledgers sit on whole UTC days."""
        body = viewer.app.test_client().get('/airtime').data.decode()
        assert 'id="airtime-ledger-span"' in body
        assert 'Les tableaux répondent pour : ' in body
        assert 'calés sur des jours UTC entiers.' in body

    def test_every_ledger_row_shows_its_share_of_the_channel_capacity(self, viewer):
        body = viewer.app.test_client().get('/airtime').data.decode()
        assert 'pct(row.capacity_percent)' in body
        assert 'le plafond est une seconde de canal par seconde écoulée' in body
        assert 'au-delà de 100 %, des émissions parallèles' in body

    def test_the_unattributed_rows_explain_themselves_in_the_cell(self, viewer):
        """Both glosses sit in the cell, not behind a hover invisible on a phone."""
        body = viewer.app.test_client().get('/airtime').data.decode()
        for gloss in (
            'Contenu sans identité émettrice sur le fil : messages, requêtes, inconnus. Chaque copie compte.',
            'Aucun nom émetteur sur le fil : non décodé ou inconnu. Chaque copie compte.',
        ):
            assert gloss in body
            assert len([word for word in re.split(r"[\s']+", gloss) if word]) <= 15

    def test_the_fold_and_the_advert_slice_are_stated_in_the_table(self, viewer):
        body = viewer.app.test_client().get('/airtime').data.decode()
        assert 'même relais, adresse tronquée à 2 octets' in body
        assert 'Annonces : part de l’airtime de la ligne passée en annonces' in body


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
