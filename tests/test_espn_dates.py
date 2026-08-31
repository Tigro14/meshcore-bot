"""Tests for ESPN local-day date span helpers used by World Cup features."""

from datetime import datetime, timezone

import pytz

from modules.utils import espn_dates_for_local_day, filter_events_local_day


class TestEspnDatesForLocalDay:
    def test_june_20_pt_spans_two_utc_days(self):
        tz = pytz.timezone("America/Los_Angeles")
        # 9:30pm PT on June 20 — evening match in progress
        now = tz.localize(datetime(2026, 6, 20, 21, 30))
        start, end, _, _ = espn_dates_for_local_day(tz, now)
        assert start == "20260620"
        assert end == "20260621"

    def test_utc_midday_single_utc_day(self):
        tz = timezone.utc
        now = datetime(2026, 6, 20, 12, 0, tzinfo=timezone.utc)
        start, end, _, _ = espn_dates_for_local_day(tz, now)
        assert start == end == "20260620"


class TestFilterEventsLocalDay:
    def test_keeps_afternoon_and_evening_drops_next_local_day(self):
        tz = pytz.timezone("America/Los_Angeles")
        now = tz.localize(datetime(2026, 6, 20, 21, 30))
        _, _, local_start_ts, local_end_ts = espn_dates_for_local_day(tz, now)

        afternoon_ts = datetime(2026, 6, 20, 21, 0, tzinfo=timezone.utc).timestamp()  # 2pm PT
        evening_ts = datetime(2026, 6, 21, 4, 0, tzinfo=timezone.utc).timestamp()    # 9pm PT
        next_day_ts = datetime(2026, 6, 21, 21, 0, tzinfo=timezone.utc).timestamp()  # 2pm PT June 21

        games = [
            {"id": "afternoon", "event_timestamp": afternoon_ts},
            {"id": "evening", "event_timestamp": evening_ts},
            {"id": "tomorrow", "event_timestamp": next_day_ts},
        ]
        filtered = filter_events_local_day(games, local_start_ts, local_end_ts)
        assert [g["id"] for g in filtered] == ["afternoon", "evening"]

    def test_keeps_events_missing_timestamp(self):
        filtered = filter_events_local_day(
            [{"id": "unknown"}], local_start_ts=0, local_end_ts=9999999999,
        )
        assert filtered == [{"id": "unknown"}]
