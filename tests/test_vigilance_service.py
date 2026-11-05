"""Tests for the Météo-France vigilance monitor (level sources, several departments, alert policy)."""

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
    fetch_vigilance_multi,
    normalize_department,
    parse_departments,
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

    def test_codes_are_normalised(self):
        assert normalize_department("2a") == "2A"
        assert normalize_department("7") == "07"

    def test_configuration_accepts_a_comma_list(self):
        assert parse_departments("75, 77,95") == ["75", "77", "95"]
        assert parse_departments(" 25 ") == ["25"]

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

    def test_a_list_needs_one_product_call_with_a_key(self):
        """The product is national, so a list costs one request, not one per department."""
        with patch(
            "modules.service_plugins.vigilance_service.requests.get", return_value=self._response()
        ) as get:
            entries = fetch_vigilance_multi(["95", "13"], "key")
        assert get.call_count == 1
        assert entries["95"]["color"] == "Orange"
        assert entries["13"]["color"] == "Jaune"
        assert entries["13"]["url"].endswith("/fr/bouches-du-rhone")

    def test_a_list_is_read_with_one_page_and_one_call_per_department(self):
        """The cookie is a session cookie, so the token is reused for every department."""
        page = Mock(status_code=200)
        page.cookies = {"mfsession": "rlz"}
        api_95 = Mock(status_code=200)
        api_95.json.return_value = SITE_API_DATA
        api_13 = Mock(status_code=200)
        api_13.json.return_value = {
            "update_time": 1762329600,
            "domain_id": "13",
            "color_max": 2,
            "timelaps": [],
        }
        with patch(
            "modules.service_plugins.vigilance_service.requests.get",
            side_effect=[page, api_95, api_13],
        ) as get:
            entries = fetch_vigilance_multi(["95", "13"], "")
        assert get.call_count == 3
        domains = [call.kwargs["params"]["domain"] for call in get.call_args_list[1:]]
        assert domains == ["95", "13"]
        # Same token for both: 'rlz' is 'eym' in ROT-13, read from the one page fetched
        assert all(
            call.kwargs["headers"]["Authorization"] == "Bearer eym"
            for call in get.call_args_list[1:]
        )
        assert entries["95"]["color"] == "Orange"
        assert entries["13"]["color"] == "Jaune"

    def test_a_list_without_a_cookie_costs_one_page_each(self):
        """Nothing to authenticate with, so every department needs its own page."""
        page_95 = Mock(status_code=200)
        page_95.cookies = {}
        page_95.content = ORANGE_HTML.encode("utf-8")
        page_25 = Mock(status_code=200)
        page_25.cookies = {}
        page_25.content = GREEN_HTML.encode("utf-8")
        with patch(
            "modules.service_plugins.vigilance_service.requests.get", side_effect=[page_95, page_25]
        ) as get:
            entries = fetch_vigilance_multi(["95", "25"], "")
        assert get.call_count == 2
        assert entries["95"]["color"] == "Orange"
        assert entries["25"]["color"] == "Vert"

    def test_empty_list_raises(self):
        with patch("modules.service_plugins.vigilance_service.requests.get") as get:
            with pytest.raises(ValueError, match="No department"):
                fetch_vigilance_multi([], "")
        get.assert_not_called()

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
            "modules.service_plugins.vigilance_service.fetch_vigilance_multi",
            return_value={"25": _info("Orange")},
        ) as fetch:
            svc._fetch_with_retry()
        assert fetch.call_args[0][1] == "key"

    def test_service_batches_a_configured_list_in_one_call(self):
        svc = _svc(api_key="key", department="75, 77, 95")
        assert svc.departments == ["75", "77", "95"]
        assert svc.department == "75"
        with patch(
            "modules.service_plugins.vigilance_service.fetch_vigilance_multi",
            return_value={code: _info("Orange") for code in svc.departments},
        ) as fetch:
            svc._fetch_with_retry()
        # The whole list goes in one call, which is what one national product call means
        assert fetch.call_args[0][0] == ["75", "77", "95"]

    def test_service_warns_when_no_key_is_configured(self):
        svc = _svc()
        assert any("api_key" in str(call) for call in svc.logger.warning.call_args_list)

    def test_overseas_department_is_left_out_of_the_poll_without_a_key(self):
        """A code the site's endpoint has no block for is named in the log and skipped."""
        svc = _svc(department="75,974")
        assert svc.departments == ["75"]
        assert any("974" in str(call) for call in svc.logger.warning.call_args_list)

    def test_overseas_department_is_polled_with_a_key(self):
        svc = _svc(department="75,974", api_key="key")
        assert svc.departments == ["75", "974"]


class TestRequestDelay:
    """Requests of one poll are spaced instead of sent as one burst."""

    def _endpoint(self, code, color):
        response = Mock(status_code=200)
        response.json.return_value = {
            "update_time": 1762329600,
            "domain_id": code,
            "color_max": color,
            "timelaps": [],
        }
        return response

    def _page(self):
        page = Mock(status_code=200)
        page.cookies = {"mfsession": "rlz"}
        return page

    def _run(self, codes, api_key, delay, responses):
        order = []

        def fake_get(*args, **kwargs):
            order.append("get")
            return next(responses)

        def fake_sleep(seconds):
            order.append(f"sleep:{seconds}")

        with patch(
            "modules.service_plugins.vigilance_service.requests.get", side_effect=fake_get
        ), patch(
            "modules.service_plugins.vigilance_service.time.sleep", side_effect=fake_sleep
        ):
            entries = fetch_vigilance_multi(codes, api_key, delay=delay)
        return order, entries

    def test_the_first_request_is_not_preceded_by_a_pause(self):
        responses = iter([self._page(), self._endpoint("95", 3), self._endpoint("13", 2)])
        order, entries = self._run(["95", "13"], "", 2.0, responses)
        assert order == ["get", "sleep:2.0", "get", "sleep:2.0", "get"]
        assert entries["95"]["color"] == "Orange"

    def test_zero_delay_sends_the_requests_back_to_back(self):
        responses = iter([self._page(), self._endpoint("95", 3), self._endpoint("13", 2)])
        order, _ = self._run(["95", "13"], "", 0.0, responses)
        assert order == ["get", "get", "get"]

    def test_pages_are_spaced_too_when_the_page_sets_no_cookie(self):
        page_95 = Mock(status_code=200)
        page_95.cookies = {}
        page_95.content = ORANGE_HTML.encode("utf-8")
        page_25 = Mock(status_code=200)
        page_25.cookies = {}
        page_25.content = GREEN_HTML.encode("utf-8")
        order, entries = self._run(["95", "25"], "", 2.0, iter([page_95, page_25]))
        assert order == ["get", "sleep:2.0", "get"]
        assert entries["25"]["color"] == "Vert"

    def test_default_is_two_seconds(self):
        assert _svc().request_delay == 2.0

    def test_delay_parsed_from_config(self):
        assert _svc(request_delay="1.5").request_delay == 1.5

    def test_service_passes_the_configured_delay(self):
        svc = _svc(request_delay="1.5")
        with patch(
            "modules.service_plugins.vigilance_service.fetch_vigilance_multi",
            return_value={"25": _info("Orange")},
        ) as fetch:
            svc._fetch_with_retry()
        assert fetch.call_args.kwargs["delay"] == 1.5


class TestFetchWithRetry:
    def test_returns_data_on_first_attempt(self):
        svc = _svc()
        with patch(
            "modules.service_plugins.vigilance_service.fetch_vigilance_multi",
            return_value={"25": _info()},
        ):
            assert svc._fetch_with_retry()["25"]["color"] == "Orange"

    def test_retries_transient_errors_then_succeeds(self):
        svc = _svc()
        errors = [
            ConnectionResetError("reset"),
            ConnectionResetError("reset"),
            {"25": _info()},
        ]
        with patch(
            "modules.service_plugins.vigilance_service.fetch_vigilance_multi", side_effect=errors
        ), patch("modules.service_plugins.vigilance_service.time.sleep") as sleep:
            data = svc._fetch_with_retry()
        assert data is not None and data["25"]["color"] == "Orange"
        assert sleep.call_count == 2

    def test_gives_up_after_three_attempts(self):
        svc = _svc()
        with patch(
            "modules.service_plugins.vigilance_service.fetch_vigilance_multi",
            side_effect=ConnectionResetError("reset"),
        ), patch("modules.service_plugins.vigilance_service.time.sleep"):
            assert svc._fetch_with_retry() is None

    def test_unexpected_error_returns_none(self):
        svc = _svc()
        with patch(
            "modules.service_plugins.vigilance_service.fetch_vigilance_multi",
            side_effect=ValueError("bad page"),
        ):
            assert svc._fetch_with_retry() is None


class TestAlertPolicy:
    def test_only_configured_levels_alert(self):
        svc = _svc()
        assert svc._should_alert("25", _info("Jaune")) is False
        assert svc._should_alert("25", _info("Orange")) is True

    def test_levels_parsed_from_config(self):
        svc = _svc(alert_levels="Jaune, Orange ,Rouge")
        assert svc.alert_levels == ["Jaune", "Orange", "Rouge"]

    def test_second_alert_is_throttled(self):
        svc = _svc()
        assert svc._should_alert("25", _info("Rouge")) is True
        svc._last_alert_time["25"] = time.monotonic()
        assert svc._should_alert("25", _info("Rouge")) is False

    def test_throttle_is_per_department(self):
        """An alert sent for one department must not hold the other ones back."""
        svc = _svc(department="25,60")
        assert svc._should_alert("25", _info("Rouge")) is True
        svc._last_alert_time["25"] = time.monotonic()
        assert svc._should_alert("25", _info("Rouge")) is False
        assert svc._should_alert("60", _info("Rouge")) is True

    def test_first_alert_of_a_run_is_not_throttled_when_monotonic_is_small(self):
        """0.0 means no alert sent yet, even when time.monotonic() starts near zero."""
        svc = _svc()
        with patch("modules.service_plugins.vigilance_service.time.monotonic", return_value=1.0):
            assert svc._should_alert("25", _info("Rouge")) is True

    def test_throttle_zero_alerts_every_time(self):
        svc = _svc(alert_throttle="0")
        svc._last_alert_time["25"] = time.monotonic()
        assert svc._should_alert("25", _info("Rouge")) is True

    def test_message_carries_color_and_the_department_that_triggered(self):
        """The message names the department it is about, not the configured one."""
        svc = _svc(department="25,60")
        message = svc._format_message("60", _info("Rouge", "Vigilance rouge : orages\n- Orages"))
        assert "VIGILANCE ROUGE" in message
        assert "Dept 60" in message
        assert "Orages" in message

    def test_message_is_capped_for_mesh(self):
        svc = _svc()
        message = svc._format_message("25", _info("Rouge", "Vigilance rouge :\n- " + "x" * 300))
        assert len(message) <= 180
        assert message.endswith("...")


class TestPollCheck:
    @pytest.mark.asyncio
    async def test_check_sends_alert_when_level_matches(self):
        svc = _svc()
        with patch("modules.service_plugins.vigilance_service.VigilanceService._fetch_with_retry", return_value={"25": _info()}):
            await svc._check_and_alert()
        assert svc._last_color["25"] == "Orange"
        svc.send_external_notifications.assert_awaited_once()
        svc.bot.command_manager.send_channel_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_one_message_per_department_at_an_alert_level(self):
        """Each department that is at an alert level gets its own message."""
        svc = _svc(department="25,60", alert_levels="Jaune,Orange,Rouge")
        infos = {"25": _info("Orange"), "60": _info("Jaune")}
        with patch("modules.service_plugins.vigilance_service.VigilanceService._fetch_with_retry", return_value=infos):
            await svc._check_and_alert()
        sent = [call.args[1] for call in svc.bot.command_manager.send_channel_message.await_args_list]
        assert len(sent) == 2
        assert any("Dept 25" in text for text in sent)
        assert any("Dept 60" in text for text in sent)

    @pytest.mark.asyncio
    async def test_level_below_alert_levels_does_not_send(self):
        svc = _svc(department="25,60")
        infos = {"25": _info("Orange"), "60": _info("Vert")}
        with patch("modules.service_plugins.vigilance_service.VigilanceService._fetch_with_retry", return_value=infos):
            await svc._check_and_alert()
        sent = [call.args[1] for call in svc.bot.command_manager.send_channel_message.await_args_list]
        assert len(sent) == 1
        assert "Dept 60" not in sent[0]

    @pytest.mark.asyncio
    async def test_check_does_nothing_when_fetch_fails(self):
        svc = _svc()
        with patch("modules.service_plugins.vigilance_service.VigilanceService._fetch_with_retry", return_value=None):
            await svc._check_and_alert()
        svc.bot.command_manager.send_channel_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_check_does_nothing_when_no_department_can_be_polled(self):
        """Overseas codes left out without a key leave nothing to fetch, so nothing is asked."""
        svc = _svc(department="974")
        with patch("modules.service_plugins.vigilance_service.VigilanceService._fetch_with_retry") as fetch:
            await svc._check_and_alert()
        fetch.assert_not_called()

    @pytest.mark.asyncio
    async def test_level_change_is_logged_per_department(self):
        svc = _svc(department="25,60")
        svc._last_color = {"25": "Vert", "60": "Orange"}
        infos = {"25": _info("Jaune"), "60": _info("Orange")}
        with patch("modules.service_plugins.vigilance_service.VigilanceService._fetch_with_retry", return_value=infos):
            await svc._check_and_alert()
        changed = [
            str(call) for call in svc.logger.info.call_args_list if "level changed" in str(call)
        ]
        assert any("25" in call and "Jaune" in call for call in changed)
        # 60 stayed Orange, so no change is reported for it
        assert not any("60" in call for call in changed)


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
