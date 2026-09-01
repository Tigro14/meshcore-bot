"""Tests for the World Cup live-score service (detection + tick flow)."""

import configparser
from unittest.mock import AsyncMock, MagicMock, Mock

from modules.service_plugins.worldcup_service import WorldCupLiveService


def _make_bot(**overrides):
    bot = MagicMock()
    bot.logger = Mock()
    config = configparser.ConfigParser()
    config.add_section("Worldcup_Service")
    config.set("Worldcup_Service", "enabled", "true")
    config.set("Worldcup_Service", "channel", "#fifa")
    for k, v in overrides.items():
        config.set("Worldcup_Service", k, v)
    bot.config = config
    # In-memory metadata store
    store = {}
    bot.db_manager = MagicMock()
    bot.db_manager.get_metadata = Mock(side_effect=lambda k: store.get(k))
    bot.db_manager.set_metadata = Mock(side_effect=lambda k, v: store.update({k: v}))
    bot.command_manager = MagicMock()
    bot.command_manager.send_channel_message = AsyncMock(return_value=True)
    return bot


def _svc(**overrides):
    svc = WorldCupLiveService(_make_bot(**overrides))
    # Avoid external-notification config lookups in _post
    svc.has_external_notification_targets = lambda: False
    svc.get_mesh_flood_scope = lambda: None
    return svc


def _match(eid="1", h=0, a=0, status="STATUS_SCHEDULED", clock="", hp=None, ap=None,
           hid="100", aid="200", hn="Côte d'Ivoire", an="Ecuador", goals=None, cards=None, yellows=None):
    return {
        "id": eid, "home_id": hid, "away_id": aid, "home_name": hn, "away_name": an,
        "home_score": h, "away_score": a, "status": status, "clock": clock,
        "home_pen": hp, "away_pen": ap, "goals": goals or [], "cards": cards or [], "yellows": yellows or [],
    }


def _goal(clock, scorer, own_goal=False, penalty=False, team_id="100", kind=""):
    return {"clock": clock, "scorer": scorer, "team_id": team_id, "own_goal": own_goal,
            "penalty": penalty, "kind": kind}


def _card(clock, player, team_id="200"):
    return {"clock": clock, "player": player, "team_id": team_id}


GROUPS = {"100": "Group E", "200": "Group E"}


class TestDetect:
    def _detect(self, svc, prev, m, in_group=True):
        committed = min(len(m.get("goals") or []), m["home_score"] + m["away_score"])
        cur = {"h": m["home_score"], "a": m["away_score"], "s": m["status"], "g": committed,
               "lg": prev.get("lg")}
        return svc._detect(prev, cur, m, in_group, GROUPS, "Round of 32")

    def test_kickoff(self):
        svc = _svc()
        prev = {"h": 0, "a": 0, "s": "STATUS_SCHEDULED"}
        m = _match(status="STATUS_FIRST_HALF")
        assert self._detect(svc, prev, m) == "Group E: Côte d'Ivoire vs Ecuador (kick-off)"

    def test_goal_detail_before_score_not_announced(self):
        # A scoring play can appear before the score updates (or while under VAR review).
        # The score is still 0-0, so do not announce a phantom goal.
        svc = _svc()
        prev = {"h": 0, "a": 0, "s": "STATUS_FIRST_HALF", "g": 0}
        m = _match(h=0, a=0, status="STATUS_FIRST_HALF", clock="17'", goals=[_goal("17'", "Lionel Messi")])
        assert self._detect(svc, prev, m) is None

    def test_goal_old_state_uses_score_baseline(self):
        # State saved before the scorer feature has no "g"; the prior score total is the
        # baseline, so a real new goal is announced (named), not replayed or dropped.
        svc = _svc()
        prev = {"h": 0, "a": 0, "s": "STATUS_FIRST_HALF"}
        m = _match(h=1, a=0, status="STATUS_FIRST_HALF", clock="23'", goals=[_goal("23'", "Erling Haaland")])
        assert self._detect(svc, prev, m) == "Group E: Côte d'Ivoire 1, Ecuador 0 (23' Erling Haaland)"

    def test_goal_names_new_scorer(self):
        svc = _svc()
        prev = {"h": 0, "a": 0, "s": "STATUS_FIRST_HALF", "g": 0}
        m = _match(h=1, a=0, status="STATUS_FIRST_HALF", clock="7'",
                   goals=[_goal("7'", "Elijah Just")])
        assert self._detect(svc, prev, m) == "Group E: Côte d'Ivoire 1, Ecuador 0 (7' Elijah Just)"

    def test_goal_header_flavor(self):
        svc = _svc()
        prev = {"h": 0, "a": 0, "s": "STATUS_FIRST_HALF", "g": 0}
        m = _match(h=1, a=0, status="STATUS_FIRST_HALF", goals=[_goal("30'", "Cody Gakpo", kind="header")])
        assert self._detect(svc, prev, m) == "Group E: Côte d'Ivoire 1, Ecuador 0 (30' Cody Gakpo, header)"

    def test_goal_penalty_annotation(self):
        svc = _svc()
        prev = {"h": 1, "a": 0, "s": "STATUS_SECOND_HALF", "g": 1}
        m = _match(h=1, a=1, status="STATUS_SECOND_HALF", clock="64'",
                   goals=[_goal("7'", "Elijah Just"), _goal("64'", "Ramin Rezaeian", penalty=True, team_id="200")])
        assert self._detect(svc, prev, m) == "Group E: Côte d'Ivoire 1, Ecuador 1 (64' Ramin Rezaeian, pen)"

    def test_goal_multiple_since_last_poll(self):
        svc = _svc()
        prev = {"h": 0, "a": 0, "s": "STATUS_FIRST_HALF", "g": 0}
        m = _match(h=2, a=0, status="STATUS_FIRST_HALF", clock="20'",
                   goals=[_goal("7'", "A. One"), _goal("20'", "B. Two")])
        assert self._detect(svc, prev, m) == "Group E: Côte d'Ivoire 2, Ecuador 0 (7' A. One; 20' B. Two)"

    def test_goal_without_details_not_announced(self):
        # Score moved but no scoring-play detail yet (ESPN lag): wait for the named detail
        # rather than posting an unnamed update now (it would double-post when it arrives).
        svc = _svc()
        prev = {"h": 0, "a": 0, "s": "STATUS_FIRST_HALF", "g": 0}
        m = _match(h=1, a=0, status="STATUS_FIRST_HALF", clock="12'")  # no goals payload yet
        assert self._detect(svc, prev, m) is None

    def test_goal_announced_when_detail_arrives_after_score(self):
        # The poll after the score ticked, the named scoring play appears -> announce it,
        # showing the (already-updated) score.
        svc = _svc()
        prev = {"h": 1, "a": 0, "s": "STATUS_FIRST_HALF", "g": 0}  # score already 1-0, no detail yet
        m = _match(h=1, a=0, status="STATUS_FIRST_HALF", clock="13'", goals=[_goal("12'", "Erling Haaland")])
        assert self._detect(svc, prev, m) == "Group E: Côte d'Ivoire 1, Ecuador 0 (12' Erling Haaland)"

    def test_disallowed_goal_names_scorer(self):
        # A previously-announced goal is rescinded (score reverts, scoring play removed).
        svc = _svc()
        prev = {"h": 1, "a": 0, "s": "STATUS_FIRST_HALF", "g": 1, "lg": "60' Lionel Messi"}
        m = _match(h=0, a=0, status="STATUS_FIRST_HALF", goals=[])
        assert self._detect(svc, prev, m) == \
            "Group E: Côte d'Ivoire 0, Ecuador 0 — 60' Lionel Messi ruled out (VAR)"

    def test_disallowed_goal_generic_when_scorer_unknown(self):
        svc = _svc()
        prev = {"h": 1, "a": 0, "s": "STATUS_FIRST_HALF", "g": 1}  # no remembered scorer
        m = _match(h=0, a=0, status="STATUS_FIRST_HALF", goals=[])
        assert self._detect(svc, prev, m) == \
            "Group E: Côte d'Ivoire 0, Ecuador 0 — goal ruled out (VAR)"

    def test_disallowed_can_be_toggled_off(self):
        svc = _svc(announce_disallowed="false")
        prev = {"h": 1, "a": 0, "s": "STATUS_FIRST_HALF", "g": 1, "lg": "60' Lionel Messi"}
        m = _match(h=0, a=0, status="STATUS_FIRST_HALF", goals=[])
        assert self._detect(svc, prev, m) is None

    def test_phantom_play_removed_is_not_a_disallowed(self):
        # A play that was never announced (committed stayed 0) being removed must NOT
        # produce a "ruled out" message.
        svc = _svc()
        prev = {"h": 0, "a": 0, "s": "STATUS_FIRST_HALF", "g": 0}
        m = _match(h=0, a=0, status="STATUS_FIRST_HALF", goals=[])
        assert self._detect(svc, prev, m) is None

    def test_halftime(self):
        svc = _svc()
        prev = {"h": 0, "a": 0, "s": "STATUS_FIRST_HALF"}
        m = _match(h=0, a=0, status="STATUS_HALFTIME")
        assert self._detect(svc, prev, m) == "Group E: Côte d'Ivoire 0, Ecuador 0 (half-time)"

    def test_fulltime(self):
        svc = _svc()
        prev = {"h": 1, "a": 1, "s": "STATUS_SECOND_HALF"}
        m = _match(h=1, a=2, status="STATUS_FULL_TIME")
        assert self._detect(svc, prev, m) == "Group E: Côte d'Ivoire 1, Ecuador 2 (full-time)"

    def test_fulltime_penalties(self):
        svc = _svc()
        prev = {"h": 2, "a": 2, "s": "STATUS_SECOND_HALF"}
        m = _match(h=2, a=2, status="STATUS_FINAL_PEN", hp=3, ap=4)
        assert self._detect(svc, prev, m, in_group=False) == \
            "Round of 32: Côte d'Ivoire 2, Ecuador 2 (full-time, pens 3-4)"

    def test_no_change_returns_none(self):
        svc = _svc()
        prev = {"h": 1, "a": 0, "s": "STATUS_FIRST_HALF"}
        m = _match(h=1, a=0, status="STATUS_FIRST_HALF")
        assert self._detect(svc, prev, m) is None

    def test_triggers_can_be_disabled(self):
        svc = _svc(announce_goals="false")
        prev = {"h": 0, "a": 0, "s": "STATUS_FIRST_HALF"}
        m = _match(h=1, a=0, status="STATUS_FIRST_HALF", clock="23'")
        assert self._detect(svc, prev, m) is None

    def test_knockout_uses_stage_label(self):
        svc = _svc()
        prev = {"h": 0, "a": 0, "s": "STATUS_SCHEDULED"}
        m = _match(status="STATUS_FIRST_HALF")
        # in_group False -> stage label instead of group
        cur = {"h": 0, "a": 0, "s": m["status"]}
        msg = svc._detect(prev, cur, m, False, GROUPS, "Round of 32")
        assert msg.startswith("Round of 32: ")


class TestRedCards:
    def _red(self, svc, prev, m, in_group=True):
        cur = {
            "h": m["home_score"], "a": m["away_score"], "s": m["status"],
            "g": min(len(m.get("goals") or []), m["home_score"] + m["away_score"]),
            "rc": len(m.get("cards") or []), "yc": len(m.get("yellows") or []), "lg": prev.get("lg"),
        }
        return svc._detect_card(prev, cur, m, in_group, GROUPS, "Round of 32",
                                count_key="rc", list_key="cards", word="red card")

    def test_red_card_names_player_and_team(self):
        svc = _svc()
        prev = {"h": 0, "a": 0, "s": "STATUS_SECOND_HALF", "rc": 0}
        m = _match(h=0, a=0, status="STATUS_SECOND_HALF", cards=[_card("80'", "Tarik Muharemovic", team_id="200")])
        assert self._red(svc, prev, m) == \
            "Group E: Côte d'Ivoire 0, Ecuador 0 — red card: Tarik Muharemovic (Ecuador, 80')"

    def test_red_card_home_team_resolved(self):
        svc = _svc()
        prev = {"h": 1, "a": 0, "s": "STATUS_SECOND_HALF", "rc": 0}
        m = _match(h=1, a=0, status="STATUS_SECOND_HALF", cards=[_card("55'", "A Defender", team_id="100")])
        assert self._red(svc, prev, m) == \
            "Group E: Côte d'Ivoire 1, Ecuador 0 — red card: A Defender (Côte d'Ivoire, 55')"

    def test_red_card_only_new_one_announced(self):
        svc = _svc()
        prev = {"h": 0, "a": 0, "s": "STATUS_SECOND_HALF", "rc": 1}
        m = _match(h=0, a=0, status="STATUS_SECOND_HALF",
                   cards=[_card("40'", "First"), _card("80'", "Second", team_id="200")])
        assert self._red(svc, prev, m) == \
            "Group E: Côte d'Ivoire 0, Ecuador 0 — red card: Second (Ecuador, 80')"

    def test_red_card_old_state_rebaselines(self):
        svc = _svc()
        prev = {"h": 0, "a": 0, "s": "STATUS_SECOND_HALF"}  # no "rc"
        m = _match(h=0, a=0, status="STATUS_SECOND_HALF", cards=[_card("80'", "Tarik Muharemovic")])
        assert self._red(svc, prev, m) is None

    def test_red_card_not_announced_when_not_in_play(self):
        svc = _svc()
        prev = {"h": 1, "a": 0, "s": "STATUS_SECOND_HALF", "rc": 0}
        m = _match(h=1, a=0, status="STATUS_FULL_TIME", cards=[_card("80'", "Late Red")])
        assert self._red(svc, prev, m) is None


class TestYellowCardsAndStoppage:
    def _yellow(self, svc, prev, m, in_group=True):
        cur = {
            "h": m["home_score"], "a": m["away_score"], "s": m["status"],
            "g": min(len(m.get("goals") or []), m["home_score"] + m["away_score"]),
            "rc": len(m.get("cards") or []), "yc": len(m.get("yellows") or []), "lg": prev.get("lg"),
        }
        return svc._detect_card(prev, cur, m, in_group, GROUPS, "Round of 32",
                                count_key="yc", list_key="yellows", word="yellow card")

    def test_yellow_card_formatted(self):
        svc = _svc(announce_yellow_cards="true")
        prev = {"h": 0, "a": 0, "s": "STATUS_FIRST_HALF", "yc": 0}
        m = _match(h=0, a=0, status="STATUS_FIRST_HALF", yellows=[_card("33'", "Teboho Mokoena", team_id="100")])
        assert self._yellow(svc, prev, m) == \
            "Group E: Côte d'Ivoire 0, Ecuador 0 — yellow card: Teboho Mokoena (Côte d'Ivoire, 33')"

    async def test_yellow_off_by_default_through_tick(self):
        svc = _svc()  # announce_yellow_cards defaults False
        assert svc.announce_yellow_cards is False
        svc.wc_data.get_active_tournament = AsyncMock(
            return_value={"league": "fifa.world", "in_group_stage": True, "stage_label": "Group"}
        )
        svc.wc_data.get_team_groups = AsyncMock(return_value=GROUPS)
        svc.espn_client.fetch_match_states = AsyncMock(return_value=[_match(h=0, a=0, status="STATUS_FIRST_HALF")])
        await svc._tick()  # seed
        svc.espn_client.fetch_match_states = AsyncMock(return_value=[
            _match(h=0, a=0, status="STATUS_FIRST_HALF", yellows=[_card("33'", "Booked")])
        ])
        await svc._tick()
        svc.bot.command_manager.send_channel_message.assert_not_awaited()

    def test_stoppage_abandoned(self):
        svc = _svc()
        prev = {"h": 1, "a": 0, "s": "STATUS_SECOND_HALF", "g": 1}
        m = _match(h=1, a=0, status="STATUS_ABANDONED")
        cur = {"h": 1, "a": 0, "s": "STATUS_ABANDONED", "g": 1, "rc": 0, "yc": 0, "lg": None}
        assert svc._detect(prev, cur, m, True, GROUPS, "Round of 32") == \
            "Group E: Côte d'Ivoire 1, Ecuador 0 (abandoned)"

    def test_stoppage_postponed_from_scheduled(self):
        svc = _svc()
        prev = {"h": 0, "a": 0, "s": "STATUS_SCHEDULED", "g": 0}
        m = _match(h=0, a=0, status="STATUS_POSTPONED")
        cur = {"h": 0, "a": 0, "s": "STATUS_POSTPONED", "g": 0, "rc": 0, "yc": 0, "lg": None}
        assert svc._detect(prev, cur, m, True, GROUPS, "Round of 32") == \
            "Group E: Côte d'Ivoire 0, Ecuador 0 (postponed)"

    def test_stoppage_not_repeated(self):
        svc = _svc()
        prev = {"h": 1, "a": 0, "s": "STATUS_SUSPENDED", "g": 1}
        m = _match(h=1, a=0, status="STATUS_SUSPENDED")
        cur = {"h": 1, "a": 0, "s": "STATUS_SUSPENDED", "g": 1, "rc": 0, "yc": 0, "lg": None}
        assert svc._detect(prev, cur, m, True, GROUPS, "Round of 32") is None


class TestTick:
    async def test_seeds_silently_then_announces(self):
        svc = _svc()
        svc.wc_data.get_active_tournament = AsyncMock(
            return_value={"league": "fifa.world", "in_group_stage": True, "stage_label": "Group"}
        )
        svc.wc_data.get_team_groups = AsyncMock(return_value=GROUPS)

        # First poll: match already in progress -> seeded, no message
        svc.espn_client.fetch_match_states = AsyncMock(return_value=[_match(h=0, a=0, status="STATUS_FIRST_HALF")])
        await svc._tick()
        svc.bot.command_manager.send_channel_message.assert_not_awaited()

        # Second poll: a goal (with scoring-play detail) -> announced with scorer
        svc.espn_client.fetch_match_states = AsyncMock(return_value=[
            _match(h=1, a=0, status="STATUS_FIRST_HALF", clock="12'", goals=[_goal("12'", "Erling Haaland")])
        ])
        await svc._tick()
        svc.bot.command_manager.send_channel_message.assert_awaited_once()
        args = svc.bot.command_manager.send_channel_message.await_args.args
        assert args[0] == "#fifa"
        assert args[1] == "Group E: Côte d'Ivoire 1, Ecuador 0 (12' Erling Haaland)"

    async def test_polls_faster_while_live(self):
        svc = _svc(live_poll_interval="20000", poll_interval="60000")
        svc.wc_data.get_active_tournament = AsyncMock(
            return_value={"league": "fifa.world", "in_group_stage": True, "stage_label": "Group"}
        )
        svc.wc_data.get_team_groups = AsyncMock(return_value=GROUPS)

        # A live match -> fast cadence
        svc.espn_client.fetch_match_states = AsyncMock(return_value=[_match(status="STATUS_FIRST_HALF")])
        assert await svc._tick() == 20.0

        # Only scheduled/finished matches -> normal cadence
        svc.espn_client.fetch_match_states = AsyncMock(return_value=[_match(status="STATUS_SCHEDULED")])
        assert await svc._tick() == 60.0

    async def test_goal_then_var_disallowed_lifecycle(self):
        svc = _svc()
        svc.wc_data.get_active_tournament = AsyncMock(
            return_value={"league": "fifa.world", "in_group_stage": True, "stage_label": "Group"}
        )
        svc.wc_data.get_team_groups = AsyncMock(return_value=GROUPS)

        # seed at 0-0
        svc.espn_client.fetch_match_states = AsyncMock(return_value=[_match(h=0, a=0, status="STATUS_FIRST_HALF")])
        await svc._tick()
        # goal -> announced, scorer remembered
        svc.espn_client.fetch_match_states = AsyncMock(return_value=[
            _match(h=1, a=0, status="STATUS_FIRST_HALF", goals=[_goal("60'", "Lionel Messi")])
        ])
        await svc._tick()
        # VAR reverses it -> separate "ruled out" notification
        svc.espn_client.fetch_match_states = AsyncMock(return_value=[_match(h=0, a=0, status="STATUS_FIRST_HALF", goals=[])])
        await svc._tick()

        posted = [c.args[1] for c in svc.bot.command_manager.send_channel_message.await_args_list]
        assert posted == [
            "Group E: Côte d'Ivoire 1, Ecuador 0 (60' Lionel Messi)",
            "Group E: Côte d'Ivoire 0, Ecuador 0 — 60' Lionel Messi ruled out (VAR)",
        ]

    async def test_goal_and_red_card_same_poll_both_announced(self):
        svc = _svc()
        svc.wc_data.get_active_tournament = AsyncMock(
            return_value={"league": "fifa.world", "in_group_stage": True, "stage_label": "Group"}
        )
        svc.wc_data.get_team_groups = AsyncMock(return_value=GROUPS)
        svc.espn_client.fetch_match_states = AsyncMock(return_value=[_match(h=0, a=0, status="STATUS_SECOND_HALF")])
        await svc._tick()  # seed
        svc.espn_client.fetch_match_states = AsyncMock(return_value=[
            _match(h=1, a=0, status="STATUS_SECOND_HALF",
                   goals=[_goal("70'", "Scorer")], cards=[_card("70'", "Sent Off", team_id="200")])
        ])
        await svc._tick()
        posted = [c.args[1] for c in svc.bot.command_manager.send_channel_message.await_args_list]
        assert posted == [
            "Group E: Côte d'Ivoire 1, Ecuador 0 (70' Scorer)",
            "Group E: Côte d'Ivoire 1, Ecuador 0 — red card: Sent Off (Ecuador, 70')",
        ]

    async def test_idle_when_no_tournament(self):
        svc = _svc()
        svc.wc_data.get_active_tournament = AsyncMock(return_value=None)
        svc.espn_client.fetch_match_states = AsyncMock()
        delay = await svc._tick()
        assert delay == svc.idle_interval_seconds
        svc.espn_client.fetch_match_states.assert_not_awaited()

    async def test_state_persisted_across_restart(self):
        bot = _make_bot()
        svc1 = WorldCupLiveService(bot)
        svc1.has_external_notification_targets = lambda: False
        svc1.get_mesh_flood_scope = lambda: None
        svc1.wc_data.get_active_tournament = AsyncMock(
            return_value={"league": "fifa.world", "in_group_stage": True, "stage_label": "Group"}
        )
        svc1.wc_data.get_team_groups = AsyncMock(return_value=GROUPS)
        svc1.espn_client.fetch_match_states = AsyncMock(return_value=[_match(h=1, a=0, status="STATUS_FIRST_HALF")])
        await svc1._tick()  # seeds 1-0

        # New instance reuses the same bot/db -> loads persisted state, no re-announce of seed
        svc2 = WorldCupLiveService(bot)
        svc2.has_external_notification_targets = lambda: False
        svc2.get_mesh_flood_scope = lambda: None
        svc2.wc_data.get_active_tournament = AsyncMock(
            return_value={"league": "fifa.world", "in_group_stage": True, "stage_label": "Group"}
        )
        svc2.wc_data.get_team_groups = AsyncMock(return_value=GROUPS)
        svc2.espn_client.fetch_match_states = AsyncMock(return_value=[_match(h=1, a=0, status="STATUS_FIRST_HALF")])
        await svc2._tick()
        svc2.bot.command_manager.send_channel_message.assert_not_awaited()
        assert svc2._state["1"]["h"] == 1
