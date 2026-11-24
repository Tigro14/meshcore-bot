"""Captured alert shapes: shared risks, exact exceptions, bounded RF messages."""

from datetime import date, datetime, time, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from modules.service_plugins.vigilance_service import _PHENOMENON_NAMES, VigilanceService
from modules.vigilance_messages import compact_alerts
from modules.vigilance_periods import PARIS, attach_period, period_key
from tests.test_vigilance_changes import Metadata, check
from tests.test_vigilance_service import _make_bot

TODAY, TOMORROW = '2026-11-15', '2026-11-16'


def stamp(day, hour=0, minute=0):
    return datetime.combine(date.fromisoformat(day), time.min, PARIS).timestamp() + hour*3600 + minute*60


NOW = stamp(TODAY, 6, 25)


def bulletin(day, risks):
    # Each tuple is an official (level, begin-hour, end-hour) window.
    colors = {1: 'Vert', 2: 'Jaune', 3: 'Orange', 4: 'Rouge'}
    maximum = max((level for windows in risks.values() for level, _, _ in windows), default=1)
    tomorrow = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
    info = {'color': colors[maximum], 'issued_at': NOW-3600, 'valid_until': stamp(tomorrow),
            'bulletin_date': datetime.fromtimestamp(NOW-3600, PARIS), 'url': '', 'summary': '',
            'phenomena': [{'id': pid, 'color': colors[max(level for level, _, _ in windows)]}
                          for pid, windows in risks.items()]}
    laps = [{'phenomenon_id': pid, 'timelaps_items': [
        {'color_id': level, 'begin_time': stamp(day, start), 'end_time': stamp(day, end)}
        for level, start, end in windows]} for pid, windows in risks.items()]
    return attach_period(info, day, laps)


def captured():
    storms = [(2, 7, 17)]
    return {
        '29': {'periods': [bulletin(TODAY, {'1': [(2, 6, 9), (2, 11, 20)], '3': storms}),
                           bulletin(TOMORROW, {'1': [(2, 18, 24)], '2': [(2, 20, 24)]})]},
        '22': {'periods': [bulletin(TODAY, {'1': [(2, 12, 20)], '3': storms}),
                           bulletin(TOMORROW, {'1': [(2, 20, 24)]})]},
        '35': {'periods': [bulletin(TODAY, {'1': [(2, 13, 20)], '3': storms}),
                           bulletin(TOMORROW, {'1': [(2, 22, 24)]})]},
        '56': {'periods': [bulletin(TODAY, {}),
                           bulletin(TOMORROW, {'1': [(2, 18, 24)], '2': [(2, 20, 24)]})]},
    }


def events(data, day):
    return [(period_key(code, day), entry, 'new') for code, info in data.items()
            for entry in info['periods'] if entry['period_date'] == day and entry['color'] != 'Vert']


def test_capture_fits_one_message_per_day_without_merging_distinct_wind_windows():
    data = captured()
    today = compact_alerts(events(data, TODAY), _PHENOMENON_NAMES, NOW, 132)
    tomorrow = compact_alerts(events(data, TOMORROW), _PHENOMENON_NAMES, NOW, 132)
    assert today == ['⚠️ Vigilance JAUNE 15/11\nVent 22:12-20h; 29:6-9h,11-20h; 35:13-20h\nOrages 22/29/35:7-17h']
    assert tomorrow == ['⚠️ Vigilance JAUNE Demain 16/11\nVent 22:20-24h; 29/56:18-24h; 35:22-24h\nPluie-inondation 29/56:20-24h']
    assert all(len(message.encode('utf-8')) <= 132 for message in today+tomorrow)
    assert '29:6-20h' not in today[0]
    assert '56' not in today[0]


def test_lower_levels_remain_explicit_within_an_orange_day():
    first = bulletin(TODAY, {'1': [(3, 8, 18)], '3': [(2, 7, 17)]})
    second = bulletin(TODAY, {'1': [(3, 8, 18)], '3': [(3, 7, 17)]})
    messages = compact_alerts([('29', first, 'new'), ('22', second, 'new')], _PHENOMENON_NAMES, NOW, 132)
    assert len(messages) == 1
    assert 'Vent 22/29:8-18h' in messages[0]
    assert 'Orages 22:7-17h' in messages[0]
    assert 'Orages jaune 29:7-17h' in messages[0]


def test_level_transitions_and_minute_precision_survive_compaction():
    data = bulletin(TODAY, {'1': [(2, 6, 8), (3, 8, 12), (2, 12, 20)]})
    data['windows'][0]['begin'] += 30*60
    messages = compact_alerts([('29', data, 'new')], _PHENOMENON_NAMES, NOW, 132)
    assert 'Vent 29:8-12h' in messages[0]
    assert 'Vent jaune 29:6h30-8h,12-20h' in messages[0]


def test_long_summaries_repeat_the_header_and_split_only_at_whole_risks_or_intervals():
    risks = {str(pid): [(2, 6, 9), (2, 11, 15), (2, 18, 24)] for pid in range(1, 10)}
    data = bulletin(TODAY, risks)
    messages = compact_alerts([(code, data, 'new') for code in ['22', '29', '35', '56']],
                              _PHENOMENON_NAMES, NOW, 90)
    assert len(messages) > 1
    assert all(message.startswith('⚠️ Vigilance JAUNE 15/11\n') for message in messages)
    assert all(len(message.encode('utf-8')) <= 90 for message in messages)
    for name in _PHENOMENON_NAMES.values():
        assert name in '\n'.join(messages)
    assert all('…' not in message and '...' not in message for message in messages)


def service(tmp_path, budget=132):
    bot = _make_bot(department='29,22,35,56', channel='#meteo', notify_on_change='true',
                    group_similar_alerts='true', include_tomorrow='true', offline_cache='true',
                    alert_levels='Jaune,Orange,Rouge', reminder_interval='86400')
    bot.db_manager = Metadata(tmp_path/'alerts.db')
    bot.command_manager.channel_body_budget.return_value = budget
    instance = VigilanceService(bot)
    instance.send_external_notifications = AsyncMock()
    return instance


async def test_service_sends_two_instead_of_six_and_restart_does_not_repeat(tmp_path):
    monitor = service(tmp_path)
    messages = await check(monitor, captured(), NOW)
    assert len(messages) == 2
    assert all(call.args[0] == '#meteo' for call in monitor.bot.command_manager.send_channel_message.await_args_list)
    assert monitor.alert_policy.state(period_key('29', TODAY))['color'] == 'Jaune'
    assert await check(service(tmp_path), captured(), NOW+3600) == []


async def test_partly_failed_summary_keeps_all_its_department_states_for_retry(tmp_path):
    monitor = service(tmp_path, 80)
    due = events(captured(), TODAY)
    monitor.bot.command_manager.send_channel_message.side_effect = [True, False]
    with patch('modules.service_plugins.vigilance_service.time.time', return_value=NOW):
        await monitor._send_grouped_alerts(due)
    assert monitor.bot.command_manager.send_channel_message.await_count == 2
    assert all(monitor.alert_policy.state(key) is None for key, _, _ in due)
    retried = service(tmp_path, 80)
    with patch('modules.service_plugins.vigilance_service.time.time', return_value=NOW+3600):
        await retried._send_grouped_alerts(due)
    assert all(retried.alert_policy.state(key)['color'] == 'Jaune' for key, _, _ in due)


def test_different_dates_or_overall_levels_cannot_share_a_summary():
    with pytest.raises(ValueError):
        compact_alerts(events(captured(), TODAY)+events(captured(), TOMORROW), _PHENOMENON_NAMES, NOW, 132)
