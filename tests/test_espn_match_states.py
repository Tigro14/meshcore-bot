"""Resilience tests for ESPNClient.fetch_match_states parsing.

A single malformed event/detail must never zero out the whole batch (that previously
silenced the World Cup live-score service entirely).
"""

from unittest.mock import Mock

from modules.clients.espn_client import ESPNClient


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def raise_for_status(self):
        pass

    async def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, payload):
        self._payload = payload
        self.closed = False
        self.last_url = None

    def get(self, url):
        self.last_url = url
        return _FakeResp(self._payload)


def _client(payload):
    return ESPNClient(logger=Mock(), session=_FakeSession(payload))


_UNSET = object()


def _event(eid, hs, a_s, status="STATUS_SECOND_HALF", details=None, comp_status=_UNSET):
    comp = {
        "competitors": [
            {"homeAway": "home", "team": {"id": "1", "displayName": "Home"}, "score": str(hs)},
            {"homeAway": "away", "team": {"id": "2", "displayName": "Away"}, "score": str(a_s)},
        ],
        "status": {"type": {"name": status}} if comp_status is _UNSET else comp_status,
        "details": details or [],
    }
    return {"id": eid, "competitions": [comp]}


def _scoring(clock, scorer="Scorer", team="1"):
    return {
        "scoringPlay": True,
        "clock": clock,
        "team": {"id": team},
        "athletesInvolved": [{"displayName": scorer}],
    }


class TestFetchMatchStatesResilience:
    async def test_null_clock_does_not_crash(self):
        # A scoring play with clock: null must not raise (it crashed the whole fetch before).
        payload = {"events": [_event("1", 1, 0, details=[_scoring(None, "Messi")])]}
        states = await _client(payload).fetch_match_states("soccer", "fifa.world")
        assert len(states) == 1
        assert states[0]["goals"][0]["scorer"] == "Messi"
        assert states[0]["goals"][0]["clock"] == ""

    async def test_one_broken_event_does_not_drop_the_rest(self):
        broken = {"id": "broken", "competitions": "not-a-list"}
        good = _event("ok", 2, 1, details=[_scoring({"displayValue": "10'"}, "Haaland")])
        states = await _client({"events": [broken, good]}).fetch_match_states("soccer", "fifa.world")
        ids = [s["id"] for s in states]
        assert ids == ["ok"]  # broken skipped, good survives
        assert states[0]["goals"][0] == {
            "clock": "10'", "scorer": "Haaland", "team_id": "1", "own_goal": False, "penalty": False, "kind": "",
        }

    async def test_null_status_defaults_to_unknown(self):
        payload = {"events": [_event("1", 0, 0, comp_status=None)]}
        states = await _client(payload).fetch_match_states("soccer", "fifa.world")
        assert states[0]["status"] == "UNKNOWN"

    async def test_missing_athletes_yields_empty_scorer(self):
        det = {"scoringPlay": True, "clock": {"displayValue": "5'"}, "team": {"id": "1"}}
        payload = {"events": [_event("1", 1, 0, details=[det])]}
        states = await _client(payload).fetch_match_states("soccer", "fifa.world")
        assert states[0]["goals"][0]["scorer"] == ""

    async def test_red_card_parsed_into_cards(self):
        red = {"redCard": True, "scoringPlay": False, "clock": {"displayValue": "80'"},
               "team": {"id": "2"}, "athletesInvolved": [{"displayName": "Tarik Muharemovic"}]}
        goal = _scoring({"displayValue": "10'"}, "Scorer")
        payload = {"events": [_event("1", 1, 0, details=[goal, red])]}
        states = await _client(payload).fetch_match_states("soccer", "fifa.world")
        assert states[0]["cards"] == [{"clock": "80'", "player": "Tarik Muharemovic", "team_id": "2"}]
        assert len(states[0]["goals"]) == 1  # the red card is not counted as a goal

    async def test_null_clock_red_card_does_not_crash(self):
        red = {"redCard": True, "scoringPlay": False, "clock": None, "team": {"id": "1"},
               "athletesInvolved": [{"displayName": "X"}]}
        states = await _client({"events": [_event("1", 0, 0, details=[red])]}).fetch_match_states("soccer", "fifa.world")
        assert states[0]["cards"][0]["clock"] == ""

    async def test_normal_event_parses(self):
        payload = {"events": [_event("1", 2, 1, details=[
            _scoring({"displayValue": "12'"}, "A"), _scoring({"displayValue": "40'"}, "B", team="2"),
        ])]}
        states = await _client(payload).fetch_match_states("soccer", "fifa.world")
        s = states[0]
        assert (s["home_score"], s["away_score"]) == (2, 1)
        assert [g["scorer"] for g in s["goals"]] == ["A", "B"]

    async def test_yellow_card_parsed_into_yellows(self):
        yel = {"yellowCard": True, "scoringPlay": False, "clock": {"displayValue": "33'"},
               "team": {"id": "1"}, "athletesInvolved": [{"displayName": "Teboho Mokoena"}]}
        states = await _client({"events": [_event("1", 0, 0, details=[yel])]}).fetch_match_states("soccer", "fifa.world")
        assert states[0]["yellows"] == [{"clock": "33'", "player": "Teboho Mokoena", "team_id": "1"}]
        assert states[0]["cards"] == []  # not a red card

    async def test_goal_kind_header(self):
        det = {"scoringPlay": True, "clock": {"displayValue": "30'"}, "team": {"id": "1"},
               "type": {"text": "Goal - Header"}, "athletesInvolved": [{"displayName": "Gakpo"}]}
        states = await _client({"events": [_event("1", 1, 0, details=[det])]}).fetch_match_states("soccer", "fifa.world")
        assert states[0]["goals"][0]["kind"] == "header"

    async def test_fetch_match_states_uses_dates_param(self):
        session = _FakeSession({"events": []})
        client = ESPNClient(logger=Mock(), session=session)
        await client.fetch_match_states(
            "soccer", "fifa.world", start_date="20260620", end_date="20260621",
        )
        assert session.last_url is not None
        assert "dates=20260620-20260621" in session.last_url

    async def test_fetch_match_states_dates_and_cache_bust(self):
        session = _FakeSession({"events": []})
        client = ESPNClient(logger=Mock(), session=session)
        await client.fetch_match_states(
            "soccer", "fifa.world", cache_bust=True, start_date="20260620", end_date="20260621",
        )
        assert "dates=20260620-20260621" in session.last_url
        assert "_=" in session.last_url
