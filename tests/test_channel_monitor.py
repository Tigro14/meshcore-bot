"""Tests for modules/web_viewer/channel_monitor.py.

The monitor-list helpers decide what the Settings page writes to
``[Channels] monitor_channels``; the QR helpers decide what the public
/channels page is allowed to print.  Both sides are tested here: the name
rules, the secret derivation (against the bot's own key generators), and the
public/admin gating of a custom channel's private key.
"""

from __future__ import annotations

import configparser
import logging

from modules.channel_manager import ChannelManager
from modules.web_viewer.channel_monitor import (
    ChannelMonitorMixin,
    admin_qr_for,
    channel_qr,
    hashtag_secret,
    is_public,
    is_valid_name,
    lookup_key,
    monitor_channel_limit,
    normalize_name,
    parse_monitor_list,
    parse_names,
    public_qr_for,
)


def _config(**sections: dict[str, str]) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    for section, values in sections.items():
        cfg.add_section(section)
        for key, value in values.items():
            cfg.set(section, key, value)
    return cfg


class _StubMonitor(ChannelMonitorMixin):
    """ChannelMonitorMixin with the channel table the real mixin borrows."""

    def __init__(self, config, channels):
        self.config = config
        self.logger = logging.getLogger("test_channel_monitor")
        self._channels = channels

    def _get_channels(self):
        return self._channels


# ---------------------------------------------------------------------------
# Name handling
# ---------------------------------------------------------------------------

class TestNormalizeName:
    def test_a_hashtag_channel_keeps_its_prefix_and_lowercases(self):
        assert normalize_name("#FR-IDF") == "#fr-idf"

    def test_a_custom_channel_stays_bare(self):
        assert normalize_name("  Secreto  ") == "secreto"

    def test_empty_and_none_collapse_to_empty_string(self):
        assert normalize_name(None) == ""
        assert normalize_name("   ") == ""

    def test_the_prefix_is_the_only_significant_case_difference(self):
        """The bot compares monitor_channels against message.channel verbatim."""
        assert normalize_name("#general") == "#general"
        assert normalize_name("#General") == normalize_name("#general")


class TestLookupKey:
    def test_lookup_ignores_the_prefix(self):
        assert lookup_key("#fr-idf") == lookup_key("fr-idf") == "fr-idf"

    def test_lookup_key_matches_the_channel_managers_cache_rule(self):
        for name in ("#general", "General", "#Flood.Alert", "secreto"):
            assert lookup_key(name) == ChannelManager._normalize_channel_name_for_lookup(name)


class TestIsValidName:
    def test_wire_names_the_bot_already_uses_are_accepted(self):
        for name in ("#fr-idf", "#Flood.Alert", "sports.sounders", "ch-1", "a_2"):
            assert is_valid_name(name), name

    def test_the_prefix_is_optional_but_a_bare_hash_is_not(self):
        assert is_valid_name("general")
        assert not is_valid_name("#")

    def test_characters_that_would_break_config_ini_or_the_mesh_are_refused(self):
        for name in ("#a b", "#a,b", "a=b", "#éè", "100%west", "#a\nb", "#x" + "y" * 31):
            assert not is_valid_name(name), name

    def test_a_name_must_start_with_alphanumeric(self):
        assert not is_valid_name("_lead")
        assert not is_valid_name(".dotted")


class TestIsPublic:
    def test_public_is_recognised_in_the_forms_config_allows(self):
        for name in ("public", "Public", "#public", "#Public", " public "):
            assert is_public(name), name

    def test_a_channel_named_after_public_is_not_public(self):
        assert not is_public("#public-fr")
        assert not is_public("")


# ---------------------------------------------------------------------------
# monitor_channels parsing
# ---------------------------------------------------------------------------

class TestParseMonitorList:
    def test_the_config_order_and_wire_spelling_survive(self):
        assert parse_monitor_list("#FR-IDF, secreto ,#fr-idf") == ["#fr-idf", "secreto"]

    def test_blank_entries_are_dropped(self):
        assert parse_monitor_list(",, ,") == []

    def test_a_list_input_is_accepted_as_is(self):
        assert parse_monitor_list(["#General", "secreto"]) == ["#general", "secreto"]

    def test_nothing_comes_out_of_nothing(self):
        assert parse_monitor_list(None) == []
        assert parse_monitor_list("") == []


class TestParseNames:
    def test_valid_names_come_back_normalized_and_deduped(self):
        names, errors = parse_names(["#FR-IDF", "#fr-idf", "Secreto"])
        assert names == ["#fr-idf", "secreto"]
        assert errors == []

    def test_a_comma_inside_a_name_is_refused_not_split(self):
        """Splitting it would silently create a channel nobody asked for."""
        names, errors = parse_names(["#a,b"])
        assert names == []
        assert errors and "comma" in errors[0]

    def test_a_newline_is_refused(self):
        names, errors = parse_names(["#a\nb"])
        assert names == []
        assert errors and "newline" in errors[0]

    def test_a_percent_is_refused(self):
        """config.ini is read with interpolation on: a bare % breaks every
        later read of [Channels], including the bot's hot reloads."""
        names, errors = parse_names(["100%west"])
        assert names == []
        assert errors and "%" in errors[0]

    def test_a_name_the_meshcore_app_would_reject_is_refused(self):
        names, errors = parse_names(["#channel name"])
        assert names == []
        assert errors and "invalid name" in errors[0]

    def test_public_is_not_an_error_here(self):
        """Whether Public is allowed depends on the [Bot] override key, which
        the caller holds; the parser must not decide it."""
        names, errors = parse_names(["public"])
        assert names == ["public"]
        assert errors == []

    def test_junk_entries_are_ignored_rather_than_reported(self):
        names, errors = parse_names(["", "  ", "#"])
        assert names == []
        assert errors == []


class TestMonitorChannelLimit:
    def test_the_limit_follows_max_channels(self):
        assert monitor_channel_limit(_config(Bot={"max_channels": "12"})) == 12

    def test_the_default_is_the_meshcore_slot_count(self):
        assert monitor_channel_limit(_config()) == 40

    def test_a_bogus_max_channels_falls_back_instead_of_raising(self):
        assert monitor_channel_limit(_config(Bot={"max_channels": "many"})) == 40
        assert monitor_channel_limit(None) == 40

    def test_the_limit_never_collapses_to_zero(self):
        """A zero-slot limit would make the settings page impossible to save."""
        assert monitor_channel_limit(_config(Bot={"max_channels": "0"})) == 1


# ---------------------------------------------------------------------------
# QR payloads
# ---------------------------------------------------------------------------

class TestHashtagSecret:
    def test_it_matches_the_rule_the_bot_uses_to_build_channel_keys(self):
        for name in ("#fr-idf", "#general", "#Flood.Alert"):
            assert hashtag_secret(name) == ChannelManager.generate_hashtag_key(name).hex()
            assert bytes.fromhex(hashtag_secret(name)) == ChannelManager.generate_hashtag_key(name)

    def test_the_prefix_is_added_before_hashing(self):
        assert hashtag_secret("general") == hashtag_secret("#general")
        assert hashtag_secret("general") != hashtag_secret("#general-x")


class TestChannelQr:
    def test_a_hashtag_channel_needs_no_stored_key(self):
        result = channel_qr("#fr-idf")["qr"]
        assert result["name"] == "fr-idf"
        assert result["secret"] == hashtag_secret("#fr-idf")
        assert result["uri"] == f"meshcore://channel/add?name=fr-idf&secret={result['secret']}"

    def test_the_app_wants_the_name_without_the_hash(self):
        assert channel_qr("#Flood.Alert")["qr"]["name"] == "flood.alert"

    def test_a_custom_channel_uses_its_stored_key(self):
        key = "ab" * 16
        result = channel_qr("secreto", key)["qr"]
        assert result["name"] == "secreto"
        assert result["secret"] == key

    def test_a_custom_channel_without_a_key_has_no_qr(self):
        assert channel_qr("secreto")["qr"] is None

    def test_a_malformed_key_produces_no_qr_rather_than_a_scannable_lie(self):
        for key in ("deadbeef", "z" * 32, "ab" * 15 + "g"):
            assert channel_qr("secreto", key)["qr"] is None, key

    def test_the_known_channel_type_beats_the_name_guess(self):
        """A node can report a bare name as a hashtag channel."""
        assert channel_qr("general", hashtag=True)["qr"]["secret"] == hashtag_secret("#general")
        assert channel_qr("#weird", "ab" * 16, hashtag=False)["qr"]["secret"] == "ab" * 16

    def test_the_name_is_percent_encoded_so_it_cannot_inject_a_second_parameter(self):
        """channel_qr is a helper: it has to hold even when the caller skipped
        parse_names, and a name is never allowed to escape the query string."""
        result = channel_qr("a b&secret=x", "ab" * 16)["qr"]
        assert "name=a%20b%26secret%3Dx" in result["uri"]
        assert result["uri"].count("secret=") == 1

    def test_an_empty_name_produces_no_qr(self):
        assert channel_qr("")["qr"] is None
        assert channel_qr(None)["qr"] is None


# ---------------------------------------------------------------------------
# Public / admin QR gating
# ---------------------------------------------------------------------------

class TestQrGating:
    HASHTAG = {"name": "#fr-idf", "type": "hashtag", "key_hex": None}
    CUSTOM_PRIVATE = {"name": "secreto", "type": "custom", "key_hex": "cd" * 16}
    CUSTOM_PUBLISHED = {"name": "opendata", "type": "custom", "key_hex": "ef" * 16,
                        "live_messages_visible": True}
    PUBLIC = {"name": "Public", "type": "custom", "key_hex": "11" * 16,
              "live_messages_visible": True}

    def test_a_hashtag_qr_is_public_because_the_secret_is_already_published(self):
        assert public_qr_for(self.HASHTAG)["secret"] == hashtag_secret("#fr-idf")

    def test_an_unpublished_custom_key_stays_private(self):
        assert public_qr_for(self.CUSTOM_PRIVATE) is None

    def test_a_published_custom_channel_gets_its_qr_to_visitors(self):
        assert public_qr_for(self.CUSTOM_PUBLISHED)["secret"] == "ef" * 16

    def test_the_public_channel_is_never_encoded(self):
        """It is conventional, not ours to distribute, and its key is not
        hashtag-derived."""
        assert public_qr_for(self.PUBLIC) is None
        assert admin_qr_for(self.PUBLIC) is None

    def test_an_admin_sees_the_private_custom_key(self):
        assert admin_qr_for(self.CUSTOM_PRIVATE)["secret"] == "cd" * 16

    def test_an_admin_sees_the_hashtag_key_too(self):
        assert admin_qr_for(self.HASHTAG)["secret"] == hashtag_secret("#fr-idf")

    def test_a_channel_the_node_has_no_key_for_gets_no_qr(self):
        """Monitored-but-not-yet-provisioned: nothing scannable until Radio creates it."""
        assert admin_qr_for({"name": "secreto", "type": "custom", "key_hex": None}) is None

    def test_the_explicit_hashtag_flag_from_monitor_view_is_what_counts(self):
        """type='hashtag' is the node's claim; the flag is the merged view's."""
        channel = {"name": "general", "hashtag": True, "type": "custom", "key_hex": "ab" * 16}
        assert public_qr_for(channel)["secret"] == hashtag_secret("#general")

    def test_no_name_means_no_qr(self):
        assert public_qr_for({"name": ""}) is None
        assert admin_qr_for({"name": None}) is None


# ---------------------------------------------------------------------------
# The merged view the two pages read
# ---------------------------------------------------------------------------

class TestMonitorView:
    def _view(self, monitored, channels, max_channels="40"):
        config = _config(
            Channels={"monitor_channels": monitored},
            Bot={"max_channels": max_channels},
        )
        return _StubMonitor(config, channels)._monitor_view()

    def test_monitored_channels_come_first_in_config_order(self):
        view = self._view("#zoulou,#fr-idf", [
            {"name": "#fr-idf", "channel_idx": 1, "type": "hashtag", "key_hex": None},
            {"name": "#zoulou", "channel_idx": 0, "type": "hashtag", "key_hex": None},
        ])
        assert [c["name"] for c in view["channels"]] == ["#zoulou", "#fr-idf"]
        assert view["monitor_channels"] == ["#zoulou", "#fr-idf"]

    def test_a_monitored_channel_absent_from_the_radio_is_still_listed(self):
        entry = self._view("#new", [])["channels"][0]
        assert entry["on_radio"] is False
        assert entry["monitored"] is True
        assert entry["channel_idx"] is None

    def test_a_monitor_name_matches_a_radio_slot_whatever_the_node_spells(self):
        entry = self._view("#fr-idf", [
            {"name": "FR-IDF", "channel_idx": 4, "type": "hashtag", "key_hex": None},
        ])["channels"][0]
        assert entry["on_radio"] is True
        assert entry["channel_idx"] == 4

    def test_radio_slots_that_are_not_monitored_are_listed_as_unmonitored(self):
        view = self._view("#fr-idf", [
            {"name": "#fr-idf", "channel_idx": 0, "type": "hashtag", "key_hex": None},
            {"name": "#heard_but_ignored", "channel_idx": 1, "type": "hashtag", "key_hex": None},
        ])
        unmonitored = [c for c in view["channels"] if not c["monitored"]]
        assert [c["name"] for c in unmonitored] == ["#heard_but_ignored"]
        assert unmonitored[0]["on_radio"] is True

    def test_a_channel_is_flagged_hashtag_by_its_type_or_its_prefix(self):
        view = self._view("#hash,#bare", [
            {"name": "#hash", "channel_idx": 0, "type": "hashtag", "key_hex": None},
            {"name": "bare", "channel_idx": 1, "type": "custom", "key_hex": "ab" * 16},
            {"name": "general", "channel_idx": 2, "type": "hashtag", "key_hex": None},
        ])
        by_name = {c["name"]: c for c in view["channels"]}
        assert by_name["#hash"]["hashtag"] is True
        assert by_name["bare"]["hashtag"] is False
        # A node that reports a hashtag channel with its prefix stripped.
        assert by_name["general"]["hashtag"] is True

    def test_the_limit_travels_with_the_view(self):
        assert self._view("#a", [], max_channels="6")["limit"] == 6

    def test_a_channel_with_no_name_is_not_merged_twice(self):
        view = self._view("#fr-idf", [
            {"name": "", "channel_idx": 0, "type": "hashtag", "key_hex": None},
            {"name": "#fr-idf", "channel_idx": 1, "type": "hashtag", "key_hex": None},
        ])
        assert [c["name"] for c in view["channels"]] == ["#fr-idf"]


class TestMissingHashtagChannels:
    def _missing(self, monitored, channels):
        config = _config(Channels={"monitor_channels": ",".join(monitored)})
        return _StubMonitor(config, channels)._missing_hashtag_channels(monitored)

    def test_a_monitored_hashtag_channel_with_no_slot_is_reported(self):
        assert self._missing(["#new"], []) == ["#new"]

    def test_a_channel_the_radio_already_hears_is_not_reported(self):
        channels = [{"name": "fr-idf", "type": "hashtag", "key_hex": None}]
        assert self._missing(["#fr-idf"], channels) == []

    def test_custom_channels_are_left_to_the_radio_page(self):
        """Creating one means choosing its key, which only Radio does."""
        assert self._missing(["secreto"], []) == []

    def test_public_is_not_provisioned(self):
        assert self._missing(["public"], []) == []

    def test_nothing_is_missing_from_an_empty_list(self):
        assert self._missing([], []) == []
