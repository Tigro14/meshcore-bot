"""Tests for the /channels page, its /api/channels/view feed, and the
monitored-channels settings editor ([Channels] monitor_channels).

The security shape is the point: the page is public, so the feed has to leak
nothing a visitor is not already allowed to know — a hashtag secret is
derivable from a published name, a custom channel's key is not.
"""

from __future__ import annotations

import configparser
import sqlite3
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from modules.config_validation import PUBLIC_CHANNEL_OVERRIDE_KEY
from modules.models import MeshMessage
from modules.web_viewer.app import BotDataViewer
from modules.web_viewer.channel_monitor import hashtag_secret
from modules.web_viewer.channel_visibility import channel_identity, set_visibility

PRIVATE_CUSTOM_KEY = "cd" * 16
PASSWORD = "secret123"


@pytest.fixture
def viewer(tmp_path):
    """BotDataViewer with auth disabled (no web_viewer_password)."""
    return _make_viewer(tmp_path, password="")


@pytest.fixture
def gated_viewer(tmp_path):
    """BotDataViewer with the admin password set, so anonymous access is separate."""
    return _make_viewer(tmp_path, password=PASSWORD)


def _make_viewer(tmp_path, password):
    config = configparser.ConfigParser()
    config.add_section("Bot")
    config.set("Bot", "db_path", str(tmp_path / "meshcore_bot.db"))
    config.set("Bot", "bot_name", "TestBot")
    config.set("Bot", "max_channels", "4")
    config.add_section("Channels")
    config.set("Channels", "monitor_channels", "#fr-idf")
    config.add_section("Web_Viewer")
    for key, value in [
        ("host", "127.0.0.1"), ("port", "8080"), ("enabled", "false"),
        ("auto_start", "false"), ("debug", "false"),
        ("cors_allowed_origins", "*"), ("web_viewer_password", password),
    ]:
        config.set("Web_Viewer", key, value)

    config_path = str(tmp_path / "config.ini")
    with open(config_path, "w", encoding="utf-8") as handle:
        config.write(handle)

    with (
        patch.object(BotDataViewer, "_start_database_polling"),
        patch.object(BotDataViewer, "_start_log_tailing"),
        patch.object(BotDataViewer, "_start_cleanup_scheduler"),
        patch.object(BotDataViewer, "_start_dashboard_refresher"),
        patch.object(BotDataViewer, "_setup_socketio_handlers"),
        patch("modules.web_viewer.app.RepeaterManager"),
    ):
        instance = BotDataViewer(
            db_path=str(tmp_path / "meshcore_bot.db"), config_path=config_path)
    instance.app.testing = True
    return instance


def _client(viewer):
    return viewer.app.test_client()


def _login(viewer):
    client = _client(viewer)
    client.post("/login", data={"password": PASSWORD})
    return client


def _seed_channels(viewer, rows):
    """rows: (channel_idx, channel_name, channel_type, channel_key_hex)."""
    with closing(sqlite3.connect(viewer.db_path)) as conn:
        for idx, name, ctype, key in rows:
            conn.execute(
                "INSERT INTO channels(channel_idx, channel_name, channel_type, channel_key_hex) "
                "VALUES (?,?,?,?)",
                (idx, name, ctype, key),
            )
        conn.commit()


def _publish(viewer, name, key_hex=None):
    """Mark a channel as published on the web, as the Radio page does."""
    with closing(sqlite3.connect(viewer.db_path)) as conn:
        set_visibility(conn, channel_identity(name, key_hex), True)
        conn.commit()


def _operations(viewer, operation_type=None):
    where = "WHERE operation_type = ?" if operation_type else ""
    params = (operation_type,) if operation_type else ()
    with closing(sqlite3.connect(viewer.db_path)) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(
            f"SELECT * FROM channel_operations {where} ORDER BY id", params)]


def _written_monitor_channels(tmp_path):
    parser = configparser.ConfigParser()
    parser.read(tmp_path / "config.ini", encoding="utf-8")
    return parser.get("Channels", "monitor_channels", fallback=None)


def _write_channel_config(viewer, values):
    """Edit [Channels] keys in config.ini on disk, the way hand-editing does."""
    parser = configparser.ConfigParser()
    parser.read(viewer.config_path, encoding="utf-8")
    for key, value in values.items():
        parser.set("Channels", key, value)
    with open(viewer.config_path, "w", encoding="utf-8") as handle:
        parser.write(handle)


def _post_settings(viewer, channels):
    return _client(viewer).post(
        "/api/channels/settings",
        json={"channels": channels},
        content_type="application/json",
        headers={"X-Requested-With": "XMLHttpRequest"},
    )


# ---------------------------------------------------------------------------
# Page access
# ---------------------------------------------------------------------------

class TestChannelsPageAccess:
    def test_the_channels_page_is_readable_without_a_password(self, gated_viewer):
        resp = _client(gated_viewer).get("/channels")
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert "Canaux" in html
        # Rendered client-side from the vendored library, not server-side.
        assert "vendor/qrcode-generator/qrcode.js" in html

    def test_the_anonymous_page_html_carries_no_channel_key(self, gated_viewer):
        _seed_channels(gated_viewer, [(1, "secreto", "custom", PRIVATE_CUSTOM_KEY)])
        html = _client(gated_viewer).get("/channels").get_data(as_text=True)
        assert PRIVATE_CUSTOM_KEY not in html
        assert hashtag_secret("#fr-idf") not in html

    def test_the_settings_editor_is_not_part_of_the_public_surface(self, gated_viewer):
        resp = _client(gated_viewer).get("/channels/settings")
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]
        # A reused redirect is how a stale login page survives a deployment.
        assert "no-cache" in resp.headers["Cache-Control"]

    def test_the_settings_editor_opens_for_an_admin(self, gated_viewer):
        resp = _login(gated_viewer).get("/channels/settings")
        assert resp.status_code == 200
        assert "Réglages des canaux" in resp.get_data(as_text=True)
        assert 'id="cs-region-save"' in resp.get_data(as_text=True)

    def test_the_page_renders_when_auth_is_disabled(self, viewer):
        resp = _client(viewer).get("/channels")
        assert resp.status_code == 200

    def test_the_nav_links_to_both_pages(self, viewer):
        html = _client(viewer).get("/channels").get_data(as_text=True)
        assert 'href="/channels"' in html
        assert 'href="/channels/settings"' in html

    def test_a_rendered_page_is_not_kept_by_the_browser(self, viewer):
        """The nav is rendered from the deployed template, and pages carry no asset
        fingerprint: a page a browser reused from before a deployment hides links
        that page no longer has."""
        resp = _client(viewer).get("/channels")
        assert "no-cache" in resp.headers["Cache-Control"]

    def test_an_api_that_sets_its_own_freshness_is_left_alone(self, viewer):
        """Revalidation must not overwrite a deliberate max-age on a feed."""
        resp = _client(viewer).get("/api/telemetry/occupancy")
        assert "max-age" in resp.headers.get("Cache-Control", "")


# ---------------------------------------------------------------------------
# The public feed
# ---------------------------------------------------------------------------

class TestChannelsViewFeed:
    def test_a_monitored_hashtag_channel_is_shared_with_a_qr(self, gated_viewer):
        _seed_channels(gated_viewer, [(0, "#fr-idf", "hashtag", hashtag_secret("#fr-idf"))])
        entry = _client(gated_viewer).get("/api/channels/view").get_json()["channels"][0]
        assert entry["name"] == "#fr-idf"
        assert entry["monitored"] is True
        assert entry["on_radio"] is True
        assert entry["qr"]["name"] == "fr-idf"
        assert entry["qr"]["secret"] == hashtag_secret("#fr-idf")
        assert entry["qr"]["uri"].startswith("meshcore://channel/add?name=fr-idf&secret=")

    def test_the_bots_region_for_the_channel_travels_in_the_qr(self, gated_viewer):
        """A join from the QR lands the app on the region the bot posts here at."""
        _write_channel_config(gated_viewer, {
            "flood_scope.fr-idf": "#idf",
            "outgoing_flood_scope_override": "#west",
        })
        entry = _client(gated_viewer).get("/api/channels/view").get_json()["channels"][0]
        assert entry["qr"]["uri"].endswith("&region_scope=idf")

    def test_the_override_scopes_every_channel_without_its_own_entry(self, gated_viewer):
        _write_channel_config(gated_viewer, {"outgoing_flood_scope_override": "#west"})
        entry = _client(gated_viewer).get("/api/channels/view").get_json()["channels"][0]
        assert entry["qr"]["uri"].endswith("&region_scope=west")

    def test_no_region_configured_and_the_qr_stays_plain(self, gated_viewer):
        entry = _client(gated_viewer).get("/api/channels/view").get_json()["channels"][0]
        assert "region_scope" not in entry["qr"]["uri"]

    def test_a_hashtag_qr_is_offered_even_before_the_radio_subscribes(self, gated_viewer):
        entry = _client(gated_viewer).get("/api/channels/view").get_json()["channels"][0]
        assert entry["on_radio"] is False
        assert entry["qr"]["secret"] == hashtag_secret("#fr-idf")

    def test_the_public_channel_is_never_in_the_anonymous_feed(self, gated_viewer):
        _seed_channels(gated_viewer, [(2, "Public", "custom", "11" * 16)])
        _publish(gated_viewer, "Public", "11" * 16)
        names = [c["name"] for c in _client(gated_viewer).get("/api/channels/view").get_json()["channels"]]
        assert "Public" not in names

    def test_a_private_custom_channel_is_absent_rather_than_name_only(self, gated_viewer):
        """Its name would invite joins nobody can complete, and its key stays private."""
        _seed_channels(gated_viewer, [(1, "secreto", "custom", PRIVATE_CUSTOM_KEY)])
        body = _client(gated_viewer).get("/api/channels/view")
        names = [c["name"] for c in body.get_json()["channels"]]
        assert "secreto" not in names
        assert PRIVATE_CUSTOM_KEY not in body.get_data(as_text=True)

    def test_a_published_custom_channel_is_shared(self, gated_viewer):
        _seed_channels(gated_viewer, [(1, "opendata", "custom", "ef" * 16)])
        _publish(gated_viewer, "opendata", "ef" * 16)
        entries = {c["name"]: c for c in _client(gated_viewer).get("/api/channels/view").get_json()["channels"]}
        assert entries["opendata"]["qr"]["secret"] == "ef" * 16
        assert entries["opendata"]["published"] is True

    def test_the_admin_feed_carries_the_private_key_and_says_so(self, gated_viewer):
        _seed_channels(gated_viewer, [(1, "secreto", "custom", PRIVATE_CUSTOM_KEY)])
        data = _login(gated_viewer).get("/api/channels/view").get_json()
        assert data["is_admin"] is True
        entries = {c["name"]: c for c in data["channels"]}
        assert entries["secreto"]["qr"]["secret"] == PRIVATE_CUSTOM_KEY

    def test_anonymous_requests_are_not_flagged_admin(self, gated_viewer):
        assert _client(gated_viewer).get("/api/channels/view").get_json()["is_admin"] is False

    def test_a_custom_channel_the_radio_has_no_key_for_has_no_qr(self, viewer):
        """Monitored but not created yet: nothing scannable, and a reason why."""
        resp = _client(viewer).post(
            "/api/channels/settings",
            json={"channels": ["secreto"]},
            content_type="application/json",
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert resp.status_code == 200
        entry = next(
            c for c in _client(viewer).get("/api/channels/view").get_json()["channels"]
            if c["name"] == "secreto"
        )
        assert entry["qr"] is None
        assert entry["share_reason"] == "private_key"

    def test_the_monitor_list_travels_with_the_channels(self, gated_viewer):
        data = _client(gated_viewer).get("/api/channels/view").get_json()
        assert data["monitor_channels"] == ["#fr-idf"]


# ---------------------------------------------------------------------------
# Channels the bot only lists
# ---------------------------------------------------------------------------

class TestListedDecodeOnlyChannels:
    """[Channels_List] and [Web_Viewer] decode_hashtag_channels put names on the page that
    the radio holds no slot for.  They stay on the page — the bot publishes them through its
    ``channels`` command and derives their hashtag key — but the feed labels them with
    ``decoded``, because a card with a QR on a public page must not read as a channel the bot
    answers on.
    """

    def _declare(self, viewer, tmp_path, keys):
        with open(tmp_path / "config.ini", "a", encoding="utf-8") as handle:
            handle.write("\n[Channels_List]\n")
            for key in keys:
                handle.write(f"{key} = Test channel\n")
        viewer.config = viewer._load_merged_config()

    def _feed(self, client):
        return {c["name"]: c for c in client.get("/api/channels/view").get_json()["channels"]}

    def test_a_listed_channel_is_shared_with_a_derived_qr(self, gated_viewer, tmp_path):
        self._declare(gated_viewer, tmp_path, ["weather"])
        entry = self._feed(_client(gated_viewer))["#weather"]
        assert entry["decoded"] is True
        assert entry["monitored"] is False
        assert entry["on_radio"] is False
        assert entry["qr"]["secret"] == hashtag_secret("#weather")

    def test_a_categorized_key_is_shared_under_its_wire_name(self, gated_viewer, tmp_path):
        self._declare(gated_viewer, tmp_path, ["sports.sounders"])
        assert "#sounders" in self._feed(_client(gated_viewer))

    def test_a_channel_the_radio_holds_is_not_flagged_decoded(self, gated_viewer, tmp_path):
        _seed_channels(gated_viewer, [(0, "#fr-idf", "hashtag", hashtag_secret("#fr-idf"))])
        self._declare(gated_viewer, tmp_path, ["weather"])
        entries = self._feed(_client(gated_viewer))
        assert entries["#fr-idf"]["decoded"] is False
        assert entries["#fr-idf"]["on_radio"] is True
        assert entries["#weather"]["decoded"] is True

    def test_a_listed_channel_that_is_also_monitored_reads_as_monitored(self, gated_viewer, tmp_path):
        self._declare(gated_viewer, tmp_path, ["fr-idf"])
        entry = self._feed(_client(gated_viewer))["#fr-idf"]
        assert entry["monitored"] is True
        assert entry["decoded"] is True
        assert entry["on_radio"] is False


# ---------------------------------------------------------------------------
# Saving the monitor list
# ---------------------------------------------------------------------------

class TestMonitorChannelsSave:
    def test_the_written_value_is_the_wire_form_the_bot_compares(self, viewer, tmp_path):
        resp = _post_settings(viewer, ["#FR-IDF", "Secreto"])
        assert resp.status_code == 200
        assert _written_monitor_channels(tmp_path) == "#fr-idf,secreto"

    def test_the_response_reports_the_saved_order(self, viewer):
        body = _post_settings(viewer, ["#zoulou", "#fr-idf"]).get_json()
        assert body["monitor_channels"] == ["#zoulou", "#fr-idf"]

    def test_a_config_reload_is_queued_and_reported_by_id(self, viewer):
        operation_id = _post_settings(viewer, ["#fr-idf"]).get_json()["reload_operation_id"]
        rows = [op for op in _operations(viewer) if op["operation_type"] == "config_reload"]
        assert [op["id"] for op in rows] == [operation_id]
        assert rows[0]["status"] == "pending"

    def test_an_empty_list_is_a_valid_write(self, viewer, tmp_path):
        assert _post_settings(viewer, []).status_code == 200
        assert _written_monitor_channels(tmp_path) == ""

    def test_anonymous_visitors_cannot_save(self, gated_viewer):
        resp = _post_settings(gated_viewer, ["#fr-idf"])
        assert resp.status_code == 401

    def test_the_channels_key_is_required(self, viewer):
        resp = _client(viewer).post(
            "/api/channels/settings", json={},
            content_type="application/json", headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert resp.status_code == 400
        assert "channels" in resp.get_json()["error"]

    def test_the_settings_get_reflects_a_write_made_on_disk(self, viewer, tmp_path):
        (tmp_path / "config.ini").write_text(
            (tmp_path / "config.ini").read_text(encoding="utf-8").replace(
                "monitor_channels = #fr-idf", "monitor_channels = #edited_elsewhere"),
            encoding="utf-8",
        )
        data = _client(viewer).get("/api/channels/settings").get_json()
        assert data["monitor_channels"] == ["#edited_elsewhere"]

    def test_the_settings_get_is_not_public(self, gated_viewer):
        assert _client(gated_viewer).get("/api/channels/settings").status_code == 401

    def test_the_settings_get_reports_the_radio_state(self, viewer):
        _seed_channels(viewer, [(0, "#fr-idf", "hashtag", hashtag_secret("#fr-idf"))])
        data = _client(viewer).get("/api/channels/settings").get_json()
        assert data["limit"] == 4
        assert data["channels"][0]["on_radio"] is True


class TestRejectedWrites:
    def test_a_name_with_a_space_is_refused(self, viewer):
        resp = _post_settings(viewer, ["#channel name"])
        assert resp.status_code == 400
        assert "nom invalide" in resp.get_json()["error"]

    def test_a_percent_is_refused_before_it_breaks_the_ini(self, viewer):
        resp = _post_settings(viewer, ["100%west"])
        assert resp.status_code == 400
        assert "%" in resp.get_json()["error"]

    def test_a_comma_inside_a_name_is_refused_rather_than_split(self, viewer):
        resp = _post_settings(viewer, ["#a,b"])
        assert resp.status_code == 400
        assert "virgule" in resp.get_json()["error"]

    def test_a_rejected_write_leaves_the_config_alone(self, viewer, tmp_path):
        _post_settings(viewer, ["#ok channel"])
        assert _written_monitor_channels(tmp_path) == "#fr-idf"

    def test_the_list_cannot_outgrow_the_radios_slots(self, viewer):
        resp = _post_settings(viewer, ["#a", "#b", "#c", "#d", "#e"])
        assert resp.status_code == 400
        assert "max_channels" in resp.get_json()["error"]

    def test_public_is_refused_without_the_override_key(self, viewer):
        resp = _post_settings(viewer, ["#fr-idf", "Public"])
        assert resp.status_code == 400
        assert "Public" in resp.get_json()["error"]

    def test_public_is_accepted_once_the_operator_has_set_the_override(self, viewer, tmp_path):
        config = configparser.ConfigParser()
        config.read(tmp_path / "config.ini", encoding="utf-8")
        config.set("Bot", PUBLIC_CHANNEL_OVERRIDE_KEY, "true")
        with open(tmp_path / "config.ini", "w", encoding="utf-8") as handle:
            config.write(handle)
        viewer.config = viewer._load_merged_config()

        assert _post_settings(viewer, ["Public"]).status_code == 200
        assert _written_monitor_channels(tmp_path) == "public"


# ---------------------------------------------------------------------------
# Auto-provisioning the radio slots
# ---------------------------------------------------------------------------

class TestChannelProvisioning:
    def test_a_monitored_hashtag_channel_is_queued_onto_a_free_slot(self, viewer):
        _seed_channels(viewer, [(0, "#fr-idf", "hashtag", hashtag_secret("#fr-idf"))])
        body = _post_settings(viewer, ["#fr-idf", "#new"]).get_json()
        provisioned = body["provisioned"]
        assert [item["name"] for item in provisioned] == ["#new"]
        assert provisioned[0]["channel_idx"] == 1
        assert provisioned[0]["operation_id"]
        queued = _operations(viewer, "add")
        assert [(op["channel_idx"], op["channel_name"]) for op in queued] == [(1, "#new")]

    def test_the_key_is_left_out_so_the_firmware_derives_it(self, viewer):
        _post_settings(viewer, ["#new"])
        queued = _operations(viewer, "add")[0]
        assert queued["channel_key_hex"] is None

    def test_a_channel_the_radio_already_hears_is_not_queued_again(self, viewer):
        _seed_channels(viewer, [(0, "fr-idf", "hashtag", hashtag_secret("#fr-idf"))])
        body = _post_settings(viewer, ["#fr-idf"]).get_json()
        assert body["provisioned"] == []
        assert not _operations(viewer, "add")

    def test_a_custom_channel_is_not_created_behind_the_operators_back(self, viewer):
        """Its key has to be chosen deliberately, on the Radio page."""
        body = _post_settings(viewer, ["secreto"]).get_json()
        assert body["provisioned"] == []
        assert body["custom_monitored"] == ["secreto"]
        assert not _operations(viewer, "add")

    def test_a_channel_with_no_free_slot_is_reported_not_silently_dropped(self, viewer):
        """Four monitored channels, four slots already taken by other channels."""
        _seed_channels(viewer, [
            (0, "#one", "hashtag", None), (1, "#two", "hashtag", None),
            (2, "#three", "hashtag", None), (3, "#four", "hashtag", None),
        ])
        body = _post_settings(viewer, ["#five", "#six", "#seven", "#eight"]).get_json()
        assert body["provisioned"] == []
        assert body["not_provisioned"] == ["#five", "#six", "#seven", "#eight"]
        assert not _operations(viewer, "add")

    def test_two_new_channels_get_two_different_slots(self, viewer):
        body = _post_settings(viewer, ["#alpha", "#beta"]).get_json()
        assert [item["channel_idx"] for item in body["provisioned"]] == [0, 1]

    def test_removing_a_channel_does_not_free_its_radio_slot(self, viewer):
        """The slot is the Radio page's to remove; this page only stops monitoring."""
        _seed_channels(viewer, [(0, "#fr-idf", "hashtag", hashtag_secret("#fr-idf"))])
        body = _post_settings(viewer, ["#other"]).get_json()
        assert body["radio_only"] == ["#fr-idf"]
        assert not _operations(viewer, "remove")

    def test_public_is_never_provisioned(self, viewer, tmp_path):
        config = configparser.ConfigParser()
        config.read(tmp_path / "config.ini", encoding="utf-8")
        config.set("Bot", PUBLIC_CHANNEL_OVERRIDE_KEY, "true")
        with open(tmp_path / "config.ini", "w", encoding="utf-8") as handle:
            config.write(handle)
        viewer.config = viewer._load_merged_config()

        body = _post_settings(viewer, ["public"]).get_json()
        assert body["provisioned"] == []
        assert not _operations(viewer, "add")


class TestChannelRegionSettings:
    @staticmethod
    def _save(client, scopes):
        return client.post(
            "/api/channels/settings/scopes", json={"scopes": scopes},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )

    @staticmethod
    def _public_qrs(viewer):
        feed = _client(viewer).get("/api/channels/view").get_json()
        return {channel["name"]: channel["qr"] for channel in feed["channels"]}

    def test_admin_save_updates_the_published_and_private_qrs(self, gated_viewer, tmp_path):
        _seed_channels(gated_viewer, [
            (0, "#fr-idf", "hashtag", hashtag_secret("#fr-idf")),
            (1, "#second", "hashtag", hashtag_secret("#second")),
            (2, "secreto", "custom", PRIVATE_CUSTOM_KEY),
        ])
        _publish(gated_viewer, "#fr-idf")
        _publish(gated_viewer, "#second")
        _write_channel_config(gated_viewer, {
            "outgoing_flood_scope_override": "#fr-bre",
            "flood_scopes": "#fr, #fr-bre, *",
        })
        assert self._save(_client(gated_viewer), {"#fr-idf": "fr"}).status_code == 401

        client = _login(gated_viewer)
        settings = client.get("/api/channels/settings").get_json()
        assert settings["default_region"] == "fr-bre"
        assert settings["regions"] == ["fr", "fr-bre"]
        assert settings["channels"][0]["region_override"] is None

        response = self._save(client, {"#fr-idf": "fr", "secreto": "#private"})
        assert response.status_code == 200
        result = response.get_json()
        assert result["settings"]["channels"][0]["region_override"] == "fr"
        assert [item["id"] for item in _operations(gated_viewer, "config_reload")] == [
            result["reload_operation_id"],
        ]
        assert _written_monitor_channels(tmp_path) == "#fr-idf"
        assert not _operations(gated_viewer, "add")
        qrs = self._public_qrs(gated_viewer)
        assert set(qrs) == {"#fr-idf", "#second"}
        assert qrs["#fr-idf"]["uri"].endswith("&region_scope=fr")
        assert qrs["#second"]["uri"].endswith("&region_scope=fr-bre")
        assert qrs["#fr-idf"]["secret"] == hashtag_secret("#fr-idf")
        admin_channels = client.get("/api/channels/view").get_json()["channels"]
        private = next(channel["qr"] for channel in admin_channels if channel["name"] == "secreto")
        assert private["uri"].endswith("&region_scope=private")
        assert private["secret"] == PRIVATE_CUSTOM_KEY
        message = MeshMessage("test", channel="#fr-idf")
        assert message.effective_outgoing_flood_scope(SimpleNamespace(config=gated_viewer.config)) == "#fr"

    def test_default_restores_inheritance_even_with_a_base_override(self, gated_viewer, tmp_path):
        _seed_channels(gated_viewer, [(0, "#fr-idf", "hashtag", hashtag_secret("#fr-idf"))])
        _publish(gated_viewer, "#fr-idf")
        _write_channel_config(gated_viewer, {
            "outgoing_flood_scope_override": "#fr-bre",
            "flood_scope.#fr-idf": "#other",
        })
        overlay = tmp_path / "local" / "config.ini"
        overlay.parent.mkdir(exist_ok=True)
        overlay.write_text("[Channels]\nmonitor_channels = #fr-idf\n", encoding="utf-8")
        client = _login(gated_viewer)
        assert self._save(client, {"#fr-idf": "*"}).status_code == 200
        assert "region_scope=" not in self._public_qrs(gated_viewer)["#fr-idf"]["uri"]
        result = self._save(client, {"#fr-idf": None}).get_json()
        assert result["settings"]["channels"][0]["region_override"] is None
        assert self._public_qrs(gated_viewer)["#fr-idf"]["uri"].endswith("&region_scope=fr-bre")
        _write_channel_config(gated_viewer, {"outgoing_flood_scope_override": "#fr"})
        assert self._public_qrs(gated_viewer)["#fr-idf"]["uri"].endswith("&region_scope=fr")

    @pytest.mark.parametrize("changes", [
        {"#ghost": "fr"}, {"#second": "brétagne"}, {"#second": "#" + "a" * 30},
        {"#second": ["fr"]}, {"#second": "fr\nstartup_flood_scope = fr"},
        {"#second": "#none"}, {"second": "fr", "#second": "fr-bre"},
    ])
    def test_bad_updates_leave_the_file_and_operation_queue_intact(self, gated_viewer, changes):
        _seed_channels(gated_viewer, [
            (0, "#fr-idf", "hashtag", hashtag_secret("#fr-idf")),
            (1, "#second", "hashtag", hashtag_secret("#second")),
        ])
        old = gated_viewer._load_merged_config().items("Channels")
        response = self._save(_login(gated_viewer), {"#fr-idf": "fr", **changes})
        assert response.status_code == 400
        assert gated_viewer._load_merged_config().items("Channels") == old
        assert not _operations(gated_viewer)
