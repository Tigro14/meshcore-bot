"""Tests for the Météo-France vigilance monitor (page parsing and alert policy)."""

import configparser
import time
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
import requests
from bs4 import BeautifulSoup

from modules.service_plugins.vigilance_service import (
    _DEPT_SLUGS,
    VigilanceService,
    _dept_slug,
    _extract_bulletin_date,
    _extract_color,
    _extract_summary,
    _normalize_color,
    _slugify,
    fetch_vigilance,
)

ORANGE_HTML = (
    '<html><body>'
    '<div class="vigilance-alert" data-color="orange">Vigilance orange en cours</div>'
    '<ul class="summary"><li><p>Vigilance orange : orages</p></li><li>Averse</li></ul>'
    '<time datetime="2026-11-03T09:00:00">03/11/2026 09:00</time>'
    '</body></html>'
)

GREEN_HTML = '<html><body><p>Pas de vigilance particulière</p></body></html>'

API_PRODUCT = {
    "meta": {"product_datetime": "2026-11-05T05:00:00Z"},
    "product": {
        "update_time": "2026-11-05T05:00:00Z",
        "domain_id": "FRA",
        "periods": [
            {
                "echeance": "J",
                "timelaps": {
                    "domain_ids": [
                        {
                            "domain_id": "95",
                            "max_color_id": 3,
                            "phenomenon_items": [
                                {"phenomenon_id": "3", "phenomenon_max_color_id": 3},
                                {"phenomenon_id": "2", "phenomenon_max_color_id": 1},
                            ],
                        },
                        {"domain_id": "13", "max_color_id": 2, "phenomenon_items": []},
                    ]
                }
            }
        ],
    },
}

# What the endpoint the site's JavaScript calls answers for a department page.
SITE_API_DATA = {
    "update_time": 1762329600,
    "domain_id": "95",
    "color_max": 3,
    "timelaps": [
        {"phenomenon_id": "3", "timelaps_items": [{"color_id": 3}]},
        {"phenomenon_id": "2", "timelaps_items": [{"color_id": 1}]},
    ],
}


def _make_bot(**overrides):
    """Mock bot with a [Vigilance_Service] section and a channel manager."""
    bot = MagicMock()
    bot.logger = Mock()
    config = configparser.ConfigParser()
    config.add_section("Connection")
    config.add_section("Bot")
    config.add_section("Channels")
    config.add_section("Vigilance_Service")
    defaults = {
        "enabled": "true",
        "department": "25",
        "channel": "#meteo",
        "poll_interval": "28800000",
        "alert_throttle": "3600",
        "alert_levels": "Orange,Rouge",
    }
    for key, value in {**defaults, **overrides}.items():
        config.set("Vigilance_Service", key, value)
    bot.config = config
    bot.command_manager = MagicMock()
    bot.command_manager.send_channel_message = AsyncMock(return_value=True)
    return bot


def _svc(**overrides):
    """Service instance with the external-notification and flood-scope lookups stubbed."""
    svc = VigilanceService(_make_bot(**overrides))
    svc.has_external_notification_targets = lambda: False
    svc.get_mesh_flood_scope = lambda: None
    svc.send_external_notifications = AsyncMock()
    return svc


def _info(color="Orange", summary="Vigilance orange : orages", url="https://vigilance.meteofrance.fr/fr/doubs"):
    return {
        "color": color,
        "summary": summary,
        "bulletin_date": datetime(2026, 11, 3, 9, 0, 0),
        "url": url,
    }


class TestColorParsing:
    def test_normalize_accepts_english_and_french(self):
        assert _normalize_color("green") == "Vert"
        assert _normalize_color("yellow") == "Jaune"
        assert _normalize_color("ROUGE") == "Rouge"
        assert _normalize_color("unknown") == "Vert"

    def test_data_color_attribute_wins(self):
        soup = BeautifulSoup(ORANGE_HTML, "html.parser")
        assert _extract_color(soup) == "Orange"

    def test_alert_class_without_data_attribute(self):
        soup = BeautifulSoup('<div class="alerte rouge active">Alerte</div>', "html.parser")
        assert _extract_color(soup) == "Rouge"

    def test_legend_classes_are_skipped(self):
        soup = BeautifulSoup(
            '<div class="legend orange">Légende</div><div class="alerte rouge active">Alerte</div>',
            "html.parser",
        )
        assert _extract_color(soup) == "Rouge"

    def test_meta_tag_fallback(self):
        soup = BeautifulSoup(
            '<html><head><meta name="vigilance" content="niveau jaune"></head><body>x</body></html>',
            "html.parser",
        )
        assert _extract_color(soup) == "Jaune"

    def test_text_fallback_for_active_alert(self):
        soup = BeautifulSoup("<html><body><p>Vigilance rouge en cours dans le Doubs</p></body></html>", "html.parser")
        assert _extract_color(soup) == "Rouge"

    def test_no_alert_text_is_green(self):
        soup = BeautifulSoup(GREEN_HTML, "html.parser")
        assert _extract_color(soup) == "Vert"

    def test_empty_page_defaults_to_green(self):
        soup = BeautifulSoup("<html><body></body></html>", "html.parser")
        assert _extract_color(soup) == "Vert"


class TestSummaryAndDate:
    def test_summary_from_list(self):
        soup = BeautifulSoup(
            '<ul class="phenomenes"><li>Vigilance orange : orages</li><li>Pluie-inondation</li></ul>',
            "html.parser",
        )
        summary = _extract_summary(soup, "Orange")
        assert summary.split("\n")[0] == "- Vigilance orange : orages"

    def test_green_summary(self):
        soup = BeautifulSoup("<html><body><p>rien</p></body></html>", "html.parser")
        assert _extract_summary(soup, "Vert") == "Pas de vigilance particulière"

    def test_bulletin_date_from_time_element(self):
        soup = BeautifulSoup(ORANGE_HTML, "html.parser")
        assert _extract_bulletin_date(soup) == datetime(2026, 11, 3, 9, 0, 0)

    def test_bulletin_date_falls_back_to_now(self):
        before = datetime.now()
        soup = BeautifulSoup("<html><body><p>x</p></body></html>", "html.parser")
        parsed = _extract_bulletin_date(soup)
        assert before <= parsed <= datetime.now()


class TestFetchVigilance:
    def test_url_uses_department_slug(self):
        response = Mock(status_code=200)
        response.cookies = {}
        response.content = ORANGE_HTML.encode("utf-8")
        with patch("modules.service_plugins.vigilance_service.requests.get", return_value=response) as get:
            data = fetch_vigilance("25")
        assert "/fr/doubs" in get.call_args[0][0]
        assert data["color"] == "Orange"
        assert data["url"].startswith("https://vigilance.meteofrance.fr/")

    def test_number_is_not_a_slug(self):
        """The page is named after the department, not after its number."""
        response = Mock(status_code=200)
        response.cookies = {}
        response.content = GREEN_HTML.encode("utf-8")
        with patch("modules.service_plugins.vigilance_service.requests.get", return_value=response) as get:
            fetch_vigilance("95")
        assert get.call_args[0][0].endswith("/fr/val-d-oise")

    def test_unknown_department_raises_before_fetching(self):
        """A number with no page would only produce a 404, so it is refused."""
        with patch("modules.service_plugins.vigilance_service.requests.get") as get:
            with pytest.raises(ValueError, match="no vigilance page"):
                fetch_vigilance("99")
        get.assert_not_called()

    def test_every_department_resolves_to_its_slug(self):
        for code, slug in _DEPT_SLUGS.items():
            assert _dept_slug(code) == slug

    def test_corsican_code_is_case_insensitive(self):
        assert _dept_slug("2a") == "corse-du-sud"

    def test_name_falls_back_to_the_folded_slug(self):
        assert _slugify("Val-d'Oise") == "val-d-oise"
        assert _slugify("Côte d'Or") == "cote-d-or"

    def test_http_error_raises(self):
        response = Mock(status_code=404)
        response.raise_for_status.side_effect = requests.exceptions.HTTPError("404 Not Found")
        with patch("modules.service_plugins.vigilance_service.requests.get", return_value=response):
            with pytest.raises(requests.exceptions.HTTPError):
                fetch_vigilance("75")


class TestFetchVigilanceSources:
    """The level is JSON: the official product with a key, the site endpoint without."""

    def _response(self):
        response = Mock(status_code=200)
        response.json.return_value = API_PRODUCT
        return response

    def test_api_key_reads_the_level_from_the_product(self):
        with patch(
            "modules.service_plugins.vigilance_service.requests.get", return_value=self._response()
        ) as get:
            data = fetch_vigilance("95", "key")
        assert data["color"] == "Orange"
        assert data["summary"] == "Vigilance orange :\n- Orages"
        assert data["bulletin_date"] == datetime(2026, 11, 5, 5, 0, 0)
        assert data["url"].endswith("/fr/val-d-oise")
        assert get.call_args.kwargs["headers"]["apikey"] == "key"

    def test_yellow_level_and_no_phenomenon(self):
        with patch(
            "modules.service_plugins.vigilance_service.requests.get", return_value=self._response()
        ):
            data = fetch_vigilance("13", "key")
        assert data["color"] == "Jaune"
        assert data["summary"] == "Vigilance jaune :"

    def test_product_without_the_department_raises(self):
        """95 is in the product, 25 is not, and both have a page."""
        with patch(
            "modules.service_plugins.vigilance_service.requests.get", return_value=self._response()
        ):
            with pytest.raises(ValueError, match="no block"):
                fetch_vigilance("25", "key")

    def test_non_json_answer_points_at_the_key(self):
        response = Mock(status_code=200)
        response.json.side_effect = requests.exceptions.JSONDecodeError("portal page", "", 0)
        with patch(
            "modules.service_plugins.vigilance_service.requests.get", return_value=response
        ):
            with pytest.raises(ValueError, match="api_key"):
                fetch_vigilance("95", "key")

    def test_the_page_cookie_is_the_token(self):
        """Without a key the level comes from the endpoint the site's JavaScript calls."""
        page = Mock(status_code=200)
        page.cookies = {"mfsession": "rlz"}
        api = Mock(status_code=200)
        api.json.return_value = SITE_API_DATA
        with patch(
            "modules.service_plugins.vigilance_service.requests.get", side_effect=[page, api]
        ) as get:
            data = fetch_vigilance("95", "")
        assert data["color"] == "Orange"
        assert data["summary"] == "Vigilance orange :\n- Orages"
        assert data["bulletin_date"] == datetime.fromtimestamp(1762329600)
        assert "/wsft/v3/warning/full" in get.call_args_list[1][0][0]
        assert get.call_args_list[1].kwargs["params"]["domain"] == "95"
        # 'rlz' is 'eym' written in ROT-13, the form the site's JavaScript reads back
        assert get.call_args_list[1].kwargs["headers"]["Authorization"] == "Bearer eym"

    def test_no_cookie_falls_back_to_the_page(self):
        response = Mock(status_code=200)
        response.cookies = {}
        response.content = ORANGE_HTML.encode("utf-8")
        with patch(
            "modules.service_plugins.vigilance_service.requests.get", return_value=response
        ) as get:
            data = fetch_vigilance("25", "")
        assert data["color"] == "Orange"
        assert "cartevigilance" not in get.call_args[0][0]

    def test_overseas_department_needs_a_key(self):
        response = Mock(status_code=200)
        response.cookies = {"mfsession": "rlz"}
        with patch(
            "modules.service_plugins.vigilance_service.requests.get", return_value=response
        ) as get:
            with pytest.raises(ValueError, match="api_key"):
                fetch_vigilance("974", "")
        assert get.call_args[0][0].endswith("/fr/la-reunion")

    def test_service_passes_the_configured_key(self):
        svc = _svc(api_key="key")
        with patch(
            "modules.service_plugins.vigilance_service.fetch_vigilance",
            return_value=_info("Orange"),
        ) as fetch:
            svc._fetch_with_retry()
        assert fetch.call_args[0][1] == "key"

    def test_service_warns_when_no_key_is_configured(self):
        svc = _svc()
        assert any("api_key" in str(call) for call in svc.logger.warning.call_args_list)


class TestFetchWithRetry:
    def test_returns_data_on_first_attempt(self):
        svc = _svc()
        with patch(
            "modules.service_plugins.vigilance_service.fetch_vigilance", return_value=_info()
        ):
            assert svc._fetch_with_retry()["color"] == "Orange"

    def test_retries_transient_errors_then_succeeds(self):
        svc = _svc()
        errors = [ConnectionResetError("reset"), ConnectionResetError("reset"), _info()]
        with patch(
            "modules.service_plugins.vigilance_service.fetch_vigilance", side_effect=errors
        ), patch("modules.service_plugins.vigilance_service.time.sleep") as sleep:
            data = svc._fetch_with_retry()
        assert data is not None and data["color"] == "Orange"
        assert sleep.call_count == 2

    def test_gives_up_after_three_attempts(self):
        svc = _svc()
        with patch(
            "modules.service_plugins.vigilance_service.fetch_vigilance",
            side_effect=ConnectionResetError("reset"),
        ), patch("modules.service_plugins.vigilance_service.time.sleep"):
            assert svc._fetch_with_retry() is None

    def test_unexpected_error_returns_none(self):
        svc = _svc()
        with patch(
            "modules.service_plugins.vigilance_service.fetch_vigilance",
            side_effect=ValueError("bad page"),
        ):
            assert svc._fetch_with_retry() is None


class TestAlertPolicy:
    def test_only_configured_levels_alert(self):
        svc = _svc()
        assert svc._should_alert(_info("Jaune")) is False
        assert svc._should_alert(_info("Orange")) is True

    def test_levels_parsed_from_config(self):
        svc = _svc(alert_levels="Jaune, Orange ,Rouge")
        assert svc.alert_levels == ["Jaune", "Orange", "Rouge"]

    def test_second_alert_is_throttled(self):
        svc = _svc()
        assert svc._should_alert(_info("Rouge")) is True
        svc._last_alert_time = time.monotonic()
        assert svc._should_alert(_info("Rouge")) is False

    def test_first_alert_of_a_run_is_not_throttled_when_monotonic_is_small(self):
        """0.0 means no alert sent yet, even when time.monotonic() starts near zero."""
        svc = _svc()
        with patch("modules.service_plugins.vigilance_service.time.monotonic", return_value=1.0):
            assert svc._should_alert(_info("Rouge")) is True

    def test_throttle_zero_alerts_every_time(self):
        svc = _svc(alert_throttle="0")
        svc._last_alert_time = time.monotonic()
        assert svc._should_alert(_info("Rouge")) is True

    def test_message_carries_color_and_department(self):
        svc = _svc()
        message = svc._format_message(_info("Rouge", "Vigilance rouge : orages\n- Orages"))
        assert "VIGILANCE ROUGE" in message
        assert "Dept 25" in message
        assert "Orages" in message

    def test_message_is_capped_for_mesh(self):
        svc = _svc()
        message = svc._format_message(_info("Rouge", "Vigilance rouge :\n- " + "x" * 300))
        assert len(message) <= 180
        assert message.endswith("...")


class TestPollCheck:
    @pytest.mark.asyncio
    async def test_check_sends_alert_when_level_matches(self):
        svc = _svc()
        with patch("modules.service_plugins.vigilance_service.VigilanceService._fetch_with_retry", return_value=_info()):
            await svc._check_and_alert()
        assert svc._last_color == "Orange"
        svc.send_external_notifications.assert_awaited_once()
        svc.bot.command_manager.send_channel_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_check_does_nothing_when_fetch_fails(self):
        svc = _svc()
        with patch("modules.service_plugins.vigilance_service.VigilanceService._fetch_with_retry", return_value=None):
            await svc._check_and_alert()
        svc.bot.command_manager.send_channel_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_level_change_is_logged(self):
        svc = _svc()
        svc._last_color = "Vert"
        with patch("modules.service_plugins.vigilance_service.VigilanceService._fetch_with_retry", return_value=_info("Jaune")):
            await svc._check_and_alert()
        logged = " ".join(str(call) for call in svc.logger.info.call_args_list)
        assert "Vert" in logged and "Jaune" in logged


class TestSendAlert:
    @pytest.mark.asyncio
    async def test_mesh_send_uses_channel_and_scope(self):
        svc = _svc()
        await svc._send_alert("test", _info())
        args = svc.bot.command_manager.send_channel_message.call_args
        assert args[0][0] == "#meteo"
        assert args[0][1] == "test"

    @pytest.mark.asyncio
    async def test_silence_mesh_keeps_only_webhooks(self):
        svc = _svc(silence_mesh_output="true")
        await svc._send_alert("test", _info())
        svc.bot.command_manager.send_channel_message.assert_not_awaited()
        svc.send_external_notifications.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_url_is_appended_for_external_notification(self):
        svc = _svc()
        await svc._send_alert("test", _info(url="https://vigilance.meteofrance.fr/fr/besancon"))
        text = svc.send_external_notifications.await_args[0][0]
        assert text.startswith("test\nhttps://vigilance.meteofrance.fr")

    @pytest.mark.asyncio
    async def test_mesh_failure_does_not_stop_webhooks(self):
        svc = _svc()
        svc.bot.command_manager.send_channel_message = AsyncMock(side_effect=Exception("not connected"))
        await svc._send_alert("test", _info())
        svc.send_external_notifications.assert_awaited_once()


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_disabled_service_does_not_start_a_task(self):
        svc = _svc()
        svc.enabled = False
        await svc.start()
        assert svc._poll_task is None

    @pytest.mark.asyncio
    async def test_start_and_stop_round_trip(self):
        svc = _svc()
        with patch("modules.service_plugins.vigilance_service.VigilanceService._check_and_alert", return_value=None):
            await svc.start()
            assert svc._poll_task is not None
            await svc.stop()
        assert svc._poll_task is None

    def test_poll_interval_ms_is_converted_to_seconds(self):
        svc = _svc(poll_interval="60000")
        assert svc.poll_interval_seconds == 60.0

    def test_config_section_name(self):
        assert VigilanceService.config_section == "Vigilance_Service"
