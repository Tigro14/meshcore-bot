"""Readable per-department alerts retain hours and independent J0/J1 states."""

from copy import deepcopy
from unittest.mock import AsyncMock, patch

import pytest

from modules.service_plugins.vigilance_service import _PHENOMENON_NAMES
from modules.vigilance_messages import department_alerts
from modules.vigilance_periods import period_key
from tests.test_vigilance_changes import check
from tests.test_vigilance_compact import NOW, TODAY, TOMORROW, bulletin, captured, service


def due(code, data):
    return [(period_key(code, info['period_date']), info, 'new') for info in data[code]['periods']
            if info['color'] != 'Vert']


def current():
    data = captured()
    data['56']['periods'][0] = bulletin(TODAY, {'1': [(2, 7, 10), (2, 12, 20)], '3': [(2, 7, 17)]})
    return data


def test_both_days_fit_one_readable_message_per_department():
    data = current()
    for code in data:
        messages = department_alerts(due(code, data), _PHENOMENON_NAMES, NOW, 132)
        assert len(messages) == 1
        assert len(messages[0].encode()) <= 132
        assert ';' not in messages[0]
        assert '\n\n' not in messages[0]
        assert '15/11\n' in messages[0] and '\nDemain 16/11\n' in messages[0]
    assert department_alerts(due('29', data), _PHENOMENON_NAMES, NOW, 132) == [
        '⚠️ Finistère (29) : JAUNE\n15/11\nVent : 6h-9h et 11h-20h\nOrages : 7h-17h'
        '\nDemain 16/11\nVent : 18h-24h\nPluie-inondation : 20h-24h']


def test_mixed_day_levels_cannot_look_like_the_same_vigilance_for_both_days():
    data = {'29': {'periods': [bulletin(TODAY, {'1': [(2, 8, 18)]}),
                              bulletin(TOMORROW, {'1': [(3, 10, 20)]})]}}
    messages = department_alerts(due('29', data), _PHENOMENON_NAMES, NOW, 132)
    assert len(messages) == 1
    assert '15/11 : JAUNE\nVent : 8h-18h' in messages[0]
    assert 'Demain 16/11 : ORANGE\nVent : 10h-20h' in messages[0]


def test_separate_intervals_and_lower_levels_stay_explicit():
    info = bulletin(TODAY, {'1': [(2, 6, 8), (3, 8, 12), (2, 12, 20)]})
    info['windows'][0]['begin'] += 1800
    messages = department_alerts([('29', info, 'new')], _PHENOMENON_NAMES, NOW, 132)
    assert 'Vent : 8h-12h' in messages[0]
    assert 'Vent jaune : 6h30-8h et 12h-20h' in messages[0]


def test_long_bulletins_keep_their_dates_and_whole_risks_in_every_radio_part():
    info = bulletin(TODAY, {str(pid): [(2, 8, 18)] for pid in range(1, 10)})
    messages = department_alerts([('29', info, 'new')], _PHENOMENON_NAMES, NOW, 80)
    assert len(messages) > 1
    assert all(m.startswith('⚠️ Finistère (29) : JAUNE\n15/11\n') for m in messages)
    assert all(len(m.encode()) <= 80 for m in messages)
    assert all(name in '\n'.join(messages) for name in _PHENOMENON_NAMES.values())
    assert all('…' not in m and '...' not in m for m in messages)


def test_reminders_keep_the_named_department_and_radio_budget():
    events = [(key, info, 'reminder') for key, info, _ in due('29', current())]
    messages = department_alerts(events, _PHENOMENON_NAMES, NOW, 132)
    assert all('Rappel Finistère (29) : JAUNE' in message and len(message.encode()) <= 132 for message in messages)
    with pytest.raises(ValueError):
        department_alerts(events+due('22', current()), _PHENOMENON_NAMES, NOW, 132)


def monitor(tmp_path, budget=132):
    instance = service(tmp_path, budget)
    instance.group_similar_alerts = False
    return instance


async def test_service_combines_due_days_into_four_department_messages(tmp_path):
    instance = monitor(tmp_path)
    messages = await check(instance, current(), NOW)
    assert len(messages) == 4
    labels = {'22': 'Côtes-d’Armor', '29': 'Finistère', '35': 'Ille-et-Vilaine', '56': 'Morbihan'}
    for code, message in zip(['22', '29', '35', '56'], messages, strict=True):
        assert message.startswith(f'⚠️ {labels[code]} ({code}) : JAUNE\n')
        assert instance.alert_policy.state(period_key(code, TODAY))['color'] == 'Jaune'
        assert instance.alert_policy.state(period_key(code, TOMORROW))['color'] == 'Jaune'
    assert await check(monitor(tmp_path), current(), NOW+3600) == []


async def test_switching_from_grouped_format_does_not_repeat_existing_alerts(tmp_path):
    await check(service(tmp_path), current(), NOW)
    instance = monitor(tmp_path)
    assert await check(instance, current(), NOW+3600) == []


async def test_green_today_does_not_close_tomorrow_or_repeat_its_forecast(tmp_path):
    instance = monitor(tmp_path)
    await check(instance, current(), NOW)
    data = current()
    data['29']['periods'][0] = bulletin(TODAY, {})
    instance = monitor(tmp_path)
    messages = await check(instance, data, NOW+3600)
    assert len(messages) == 1 and 'Finistère (29)' in messages[0] and 'retour au vert' in messages[0]
    assert 'Demain' not in messages[0]
    assert instance.alert_policy.state(period_key('29', TOMORROW))['color'] == 'Jaune'


async def test_partial_failure_does_not_mark_either_day_as_sent(tmp_path):
    instance = monitor(tmp_path, 80)
    instance.bot.command_manager.send_channel_message = AsyncMock(side_effect=[True, False])
    with patch('modules.service_plugins.vigilance_service.time.time', return_value=NOW):
        await instance._send_department_alerts(due('29', deepcopy(current())))
    assert instance.bot.command_manager.send_channel_message.await_count == 2
    assert instance.alert_policy.state(period_key('29', TODAY)) is None
    assert instance.alert_policy.state(period_key('29', TOMORROW)) is None
