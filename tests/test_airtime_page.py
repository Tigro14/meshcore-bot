"""Tests for the /airtime page.

The page is the reader for [Channel_Occupancy]; before it existed the feature had
two API routes and no screen, so a percentage could only be read with curl. What
these tests protect is the honesty of the rendering: modelled airtime is stated as
modelled, rx is never merged with tx, and an absent bucket is a gap rather than a
zero.
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
    def test_page_renders(self, viewer):
        resp = viewer.app.test_client().get("/airtime")
        assert resp.status_code == 200

    def test_page_says_the_airtime_is_modelled(self, viewer):
        """The page cannot present a computed figure as a measurement of the channel."""
        body = viewer.app.test_client().get("/airtime").data.decode()
        assert "modelled" in body
        assert "CAD" in body

    def test_page_keeps_rx_and_tx_apart(self, viewer):
        """rx is a local observation, tx is what the bot decides about: never one number."""
        body = viewer.app.test_client().get("/airtime").data.decode()
        assert "rx" in body and "tx" in body
        assert "the two shares are kept apart" in body.lower() or "kept apart" in body.lower()

    def test_page_owns_parallel_traffic_and_the_floor(self, viewer):
        """A share above 100% is a fact about one shared channel, and the floor is a floor.

        Flood repetitions are separate transmissions, and a heard tree proves copies
        that never reached this antenna; a page that clamped the share would hide the
        only sign that the channel is saturated.
        """
        body = viewer.app.test_client().get("/airtime").data.decode()
        assert "flood repetitions included" in body
        assert "floor" in body.lower() and "ceiling" in body.lower()
        assert "≥ " in body

    def test_senders_table_names_the_unattributed_bucket(self, viewer):
        """Every heard copy is accounted for; a row with no identity admits it instead of inventing a name."""
        body = viewer.app.test_client().get("/airtime").data.decode()
        assert "unattributed row" in body.lower()
        assert "Unattributed" in body

    def test_the_unattributed_row_explains_itself_in_the_cell(self, viewer):
        """The bucket's name is not its meaning, and hover-only text is invisible on a phone.

        The gloss sits under the label, in the reader's language, and has to stay short
        enough not to turn a table cell into a paragraph — hence the 15-word cap.
        """
        body = viewer.app.test_client().get("/airtime").data.decode()
        gloss = (
            "Contenu sans identité émettrice sur le fil : messages, requêtes, "
            "inconnus. Chaque copie compte."
        )
        assert gloss in body
        assert len([word for word in re.split(r"[\s']+", gloss) if word]) <= 15

    def test_senders_table_shares_out_the_advert_part(self, viewer):
        """Presence traffic and talking traffic are different costs inside one sender.

        A repeater flooding adverts racks up airtime without being chatty, so the
        table shows each sender's advert share; a sender predating the split shows a
        dash there, because a 0% would claim it never advertised.
        """
        body = viewer.app.test_client().get("/airtime").data.decode()
        assert "<th>Advert</th>" in body
        assert "advert_percent" in body
        assert "share of that sender's airtime spent on adverts" in body

    def test_emitters_table_charges_the_channel_to_the_transmitter(self, viewer):
        """Occupancy asks who puts copies on the air, and MeshCore answers on the wire.

        The flood path's last node address names the relay that physically carried
        each heard copy; the page has to show that ledger next to the senders', say
        the two must never be summed, and keep a node address a prefix unless exactly
        one tracked contact matches it.
        """
        body = viewer.app.test_client().get("/airtime").data.decode()
        assert "Emitters by airtime" in body
        assert 'id="oc-emitters"' in body
        assert "top_emitters" in body
        assert "must never be summed" in body
        assert "shown as a name only when exactly one" in body

    def test_the_emitter_row_without_a_name_explains_itself_in_the_cell(self, viewer):
        """Same rule as the senders' unattributed row: the meaning sits in the cell,
        in the reader's language, short enough not to turn a table row into a block."""
        body = viewer.app.test_client().get("/airtime").data.decode()
        gloss = (
            "Aucun nom émetteur sur le fil : non décodé ou inconnu. "
            "Chaque copie compte."
        )
        assert gloss in body
        assert len([word for word in re.split(r"[\s']+", gloss) if word]) <= 15

    def test_the_tables_follow_the_selected_history_period(self, viewer):
        """A station table beside a 24h–90d selector must say which period it sums:
        a cumulative counter under a period label invents a quietness (or a share)
        the data never recorded, so both captions name the window and its limits."""
        body = viewer.app.test_client().get("/airtime").data.decode()
        assert "The table follows the history period selected above" in body
        assert "this one covers the history" in body
        # Day-aligned, and honest about how far back day detail goes.
        assert "aligned to whole UTC days" in body
        assert "reaches back only to when this breakdown" in body

    def test_the_emitter_caption_explains_the_two_address_widths(self, viewer):
        """One relay, two labels: the wire names it 2 or 3 bytes wide depending on
        the packet, and a reader who does not know that sees two emitters where the
        mesh has one. The fold rule belongs on the page, in the caption and in the
        cell, not only in the code."""
        body = viewer.app.test_client().get("/airtime").data.decode()
        assert "2 bytes wide in short packets" in body
        assert "the fold would pick a station the wire did not name" in body
        # The cell itself states which short labels were merged, in the reader's language.
        assert "même relais, adresse tronquée à 2 octets" in body

    def test_relays_table_names_where_relayed_copies_entered_the_mesh(self, viewer):
        """The flood path answers a third question: its FIRST entry is the gateway a
        relayed copy entered the mesh through. The page has to show that ledger, say
        it is neither the author nor the emitter, say direct copies are absent from
        it on purpose, and keep the three tables unsummed."""
        body = viewer.app.test_client().get("/airtime").data.decode()
        assert "Entry relays by airtime" in body
        assert 'id="oc-relays"' in body
        assert "top_relays" in body
        assert "renderRelays" in body
        assert "FIRST entry is the first relay" in body
        assert "It is not the author" in body
        assert "belong to no row here" in body

    def test_the_relay_caption_keeps_the_three_ledgers_apart(self, viewer):
        """Three tables of the same copies: one fact each, never a sum.

        The emitter caption already forbids summing two tables; with a third, the
        relay caption carries the same rule and the same unique-match condition on
        prefix labels, so no table can silently resolve an address the wire only
        half-named."""
        body = viewer.app.test_client().get("/airtime").data.decode()
        relay_caption = body.split('id="oc-relays"', 1)[1].split("</p>", 1)[0]
        assert "must never be summed" in relay_caption
        assert "exactly one known contact matches" in relay_caption
        assert "aligned to whole UTC days" in relay_caption
        # The French half of the caption, for the reader who never switches language.
        assert "jamais leur auteur ni l'antenne émettrice" in relay_caption

    def test_every_airtime_cell_shows_the_share_of_the_period(self, viewer):
        """Next to the raw seconds, every row of the three airtime tables shows
        its share of the period, at one decimal — and a dash, not a 0.0%, when
        the share is unknown."""
        body = viewer.app.test_client().get("/airtime").data.decode()
        for ledger in ("sender", "emitter", "entry-relay"):
            assert f"Share of all {ledger} airtime recorded in the selected period" in body
        # One decimal on each of the three tables, and the null case renders as a dash.
        assert body.count("Number(row.airtime_percent).toFixed(1)") == 3
        assert "row.airtime_percent == null" in body

    def test_chart_draws_an_absent_bucket_as_a_gap(self, viewer):
        """A window with no written bucket must not render as 0.0."""
        body = viewer.app.test_client().get("/airtime").data.decode()
        assert "spanGaps: false" in body
        assert "null" in body

    def test_page_prices_the_flood_copies_and_bounds_the_claim(self, viewer):
        """Counting repeats is half the answer; the page also has to price them.

        The copy share is measured over identity-covered airtime only, and the
        recorder's ring buffer bounds it: both limits belong on the page, and a
        period predating the split reads as unknown, never as a calm channel.
        """
        body = viewer.app.test_client().get("/airtime").data.decode()
        assert 'id="oc-copy"' in body
        assert "identity-covered rx airtime" in body
        assert "flood-free channel" in body
        assert "identity-covered airtime spent on copies" in body
        assert "_copy_percent" in body
        assert "(bounded)" in body
        assert "bounds, not a census" in body

    def test_the_history_trend_compares_equal_periods_only(self, viewer):
        """A change against an unrecorded past is an invented change.

        The line under the chart states a difference only when the equal period
        before holds recorded buckets; otherwise it says so instead of letting a
        fresh start read as a sudden flood.
        """
        body = viewer.app.test_client().get("/airtime").data.decode()
        assert 'id="ocTrend"' in body
        assert "Compared with the equal period before" in body
        assert "no change can be stated against an unrecorded past" in body
        assert "previous" in body

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
