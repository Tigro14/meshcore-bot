"""Broadcast policy: real SQLite persistence, simulated time and captured radio sends."""

import json
import sqlite3
from copy import deepcopy
from datetime import datetime
from unittest.mock import AsyncMock, Mock, patch

import pytest

from modules.service_plugins.vigilance_service import (
    VigilanceService,
    _parse_api_datetime,
    _product_entry,
    _read_color,
    fetch_vigilance_site_api,
)
from modules.vigilance_alert_policy import VigilanceAlertPolicy, fingerprint
from tests.test_vigilance_service import API_PRODUCT, SITE_API_DATA, _make_bot


class Metadata:
    def __init__(self, path):
        self.path = path
        with sqlite3.connect(path) as conn:
            conn.execute('CREATE TABLE IF NOT EXISTS bot_metadata (key TEXT PRIMARY KEY, value TEXT)')

    def get_metadata(self, key):
        with sqlite3.connect(self.path) as conn:
            row = conn.execute('SELECT value FROM bot_metadata WHERE key=?', (key,)).fetchone()
            return row[0] if row else None

    def set_metadata(self, key, value):
        with sqlite3.connect(self.path) as conn:
            conn.execute('INSERT OR REPLACE INTO bot_metadata VALUES (?,?)', (key, value))


def info(color='Jaune', phenomena=None):
    return {'color': color, 'phenomena': phenomena if phenomena is not None else [
        {'id': '1', 'color': color}] if color != 'Vert' else [],
        'summary': '', 'bulletin_date': datetime(2026, 11, 9, 6), 'url': ''}


@pytest.fixture
def monitor(tmp_path):
    db = Metadata(tmp_path/'state.db')
    def make(**overrides):
        bot = _make_bot(department='29,22,35,56', channel='#bretagne', poll_interval='3600000',
                        alert_levels='Jaune,Orange,Rouge', notify_on_change='true',
                        reminder_interval='86400', **overrides)
        bot.db_manager = db
        service = VigilanceService(bot)
        service.send_external_notifications = AsyncMock()
        return service
    return make


async def check(service, data, now=1794229200):
    with patch.object(service, '_fetch_with_retry', return_value=data), patch(
            'modules.service_plugins.vigilance_service.time.time', return_value=now):
        await service._check_and_alert()
    return [call.args[1] for call in service.bot.command_manager.send_channel_message.await_args_list]


async def test_all_green_startup_and_restart_remain_silent(monitor):
    data={code: info('Vert') for code in ['29','22','35','56']}
    assert await check(monitor(), data)==[]
    assert await check(monitor(), data)==[]


async def test_first_non_green_and_unchanged_hourly_checks(monitor):
    service=monitor()
    assert len(await check(service, {'29':info()}))==1
    await check(service, {'29':info()}, now=1794229200+3600)
    assert service.bot.command_manager.send_channel_message.await_count==1
    assert service.bot.command_manager.send_channel_message.await_args.args[0]=='#bretagne'


async def test_new_bulletin_without_risk_changes_does_not_repeat(monitor):
    service=monitor()
    await check(service, {'29':info()})
    newer=info();newer['bulletin_date']=datetime(2026,11,9,16)
    await check(service, {'29':newer}, now=1794229200+3600)
    assert service.bot.command_manager.send_channel_message.await_count==1


@pytest.mark.parametrize('color',['Jaune','Orange','Rouge'])
async def test_every_non_green_level_can_start_an_alert(monitor,color):
    assert len(await check(monitor(), {'29':info(color)}))==1


async def test_color_changes_bypass_the_old_hourly_throttle(monitor):
    service=monitor(alert_throttle='86400')
    await check(service, {'29':info('Jaune')})
    await check(service, {'29':info('Orange')}, now=1794229260)
    await check(service, {'29':info('Rouge')}, now=1794229320)
    await check(service, {'29':info('Orange')}, now=1794229380)
    assert service.bot.command_manager.send_channel_message.await_count==4


async def test_changed_phenomenon_or_its_level_triggers_an_update(monitor):
    service=monitor()
    first=info('Orange',[{'id':'1','color':'Orange'},{'id':'2','color':'Jaune'}])
    await check(service, {'29':first})
    second=info('Orange',[{'id':'1','color':'Orange'},{'id':'2','color':'Orange'}])
    await check(service, {'29':second},now=1794232800)
    third=info('Orange',[{'id':'1','color':'Orange'},{'id':'3','color':'Jaune'}])
    messages=await check(service, {'29':third},now=1794236400)
    assert len(messages)==3 and 'Orages' in messages[-1]


def test_phenomenon_order_is_not_a_change():
    data=info('Orange',[{'id':'1','color':'Orange'},{'id':'2','color':'Jaune'}])
    reordered=deepcopy(data);reordered['phenomena'].reverse()
    assert fingerprint(data)==fingerprint(reordered)


async def test_one_reminder_after_24_hours_since_last_success(monitor):
    service=monitor();start=1794229200
    await check(service, {'29':info()},start)
    await check(service, {'29':info()},start+23*3600)
    messages=await check(service, {'29':info()},start+86400)
    await check(service, {'29':info()},start+90000)
    assert len(messages)==2 and 'Rappel' in messages[-1]
    assert service.bot.command_manager.send_channel_message.await_count==2


async def test_restart_remembers_success_and_the_reminder_deadline(monitor):
    start=1794229200
    await check(monitor(), {'29':info('Orange')},start)
    service=monitor()
    assert await check(service, {'29':info('Orange')},start+3600)==[]
    messages=await check(service, {'29':info('Orange')},start+86400)
    assert len(messages)==1 and 'Rappel' in messages[0]


async def test_green_only_closes_a_previously_transmitted_alert_once(monitor):
    service=monitor()
    await check(service, {'29':info('Orange')})
    service=monitor()
    messages=await check(service, {'29':info('Vert')},1794232800)
    assert len(messages)==1 and 'Fin de vigilance' in messages[0]
    service=monitor()
    assert await check(service, {'29':info('Vert')},1794236400)==[]
    assert len(await check(service, {'29':info()},1794240000))==1


async def test_failed_send_is_retried_and_does_not_claim_a_delivered_alert(monitor):
    service=monitor();service.bot.command_manager.send_channel_message.return_value=False
    await check(service, {'29':info('Orange')})
    assert service.alert_policy.state('29') is None
    service=monitor()
    assert await check(service, {'29':info('Vert')})==[]
    assert len(await check(service, {'29':info('Orange')},1794232800))==1


async def test_failed_daily_reminder_does_not_move_its_deadline(monitor):
    service=monitor();start=1794229200
    await check(service,{'29':info()},start)
    service.bot.command_manager.send_channel_message.return_value=False
    await check(service,{'29':info()},start+86400)
    assert service.alert_policy.state('29')['sent_at']==start
    restarted=monitor()
    assert 'Rappel' in (await check(restarted,{'29':info()},start+90000))[0]


async def test_external_failure_does_not_repeat_a_successful_radio_alert(monitor):
    service=monitor();service.send_external_notifications.side_effect=OSError('offline')
    await check(service,{'29':info()})
    assert service.alert_policy.state('29')['color']=='Jaune'
    assert await check(monitor(),{'29':info()},1794232800)==[]


async def test_automated_alerts_keep_bot_and_channel_rate_limits(monitor):
    service=monitor()
    await check(service,{'29':info()})
    # This skips the human-command cooldown, not the radio/channel limiters.
    assert service.bot.command_manager.send_channel_message.await_args.kwargs['skip_user_rate_limit'] is True


async def test_failed_clear_keeps_the_active_state_for_retry(monitor):
    service=monitor()
    await check(service, {'29':info('Orange')})
    service.bot.command_manager.send_channel_message.return_value=False
    await check(service, {'29':info('Vert')},1794232800)
    assert service.alert_policy.state('29')['color']=='Orange'
    restarted=monitor()
    assert 'Fin de vigilance' in (await check(restarted, {'29':info('Vert')},1794236400))[0]


async def test_departments_keep_independent_states(monitor):
    service=monitor()
    await check(service, {'29':info(),'22':info('Orange'),'35':info('Vert'),'56':info('Vert')})
    messages=await check(service, {'29':info(),'22':info('Vert'),'35':info('Rouge')},1794232800)
    assert len(messages)==4
    assert 'Dept 22' in messages[-2] and 'Fin de vigilance' in messages[-2]
    assert 'Dept 35' in messages[-1] and 'ROUGE' in messages[-1]


async def test_fetch_failure_and_expired_green_never_clear_an_alert(monitor):
    service=monitor()
    await check(service, {'29':info('Orange')})
    await check(service, None,1794232800)
    expired=info('Vert');expired['valid_until']=1794232800
    await check(service, {'29':expired},1794236400)
    await check(service, {'29':{'color':'Unknown','bulletin_date':None}},1794236400)
    assert service.bot.command_manager.send_channel_message.await_count==1
    assert service.alert_policy.state('29')['color']=='Orange'


def test_target_channel_changes_get_their_own_state(tmp_path):
    db=Metadata(tmp_path/'targets.db');logger=Mock()
    first=VigilanceAlertPolicy(db,logger,'#bretagne')
    first.record_sent('29',info(),1794229200)
    other=VigilanceAlertPolicy(db,logger,'#autre')
    assert other.reason('29',info(),['Jaune'],1794232800)=='new'


@pytest.mark.parametrize('bad',[None,'',0,5,True,-1,'Green',1.0])
def test_missing_or_invalid_levels_are_not_green(bad):
    with pytest.raises(ValueError): _read_color(bad)


@pytest.mark.parametrize('value',[None,'','bad'])
def test_missing_publication_time_is_not_replaced_by_now(value):
    with pytest.raises(ValueError): _parse_api_datetime(value)


def test_product_missing_its_level_is_rejected():
    data=deepcopy(API_PRODUCT['product'])
    data['periods'][0]['timelaps']['domain_ids'][0].pop('max_color_id')
    with pytest.raises(ValueError): _product_entry('95',data)


@pytest.mark.parametrize('field',['domain_id','color_max','update_time'])
def test_incomplete_site_data_is_rejected(field):
    data=deepcopy(SITE_API_DATA);data.pop(field)
    response=Mock();response.json.return_value=data
    with patch('modules.service_plugins.vigilance_service.requests.get',return_value=response):
        with pytest.raises(ValueError): fetch_vigilance_site_api('95','token','https://example.test')


def test_malformed_persisted_state_is_ignored_without_a_false_clear(tmp_path):
    db=Metadata(tmp_path/'bad.db');policy=VigilanceAlertPolicy(db,Mock(),'#bretagne')
    db.set_metadata(policy._key('29'),json.dumps({'color':'Orange','sent_at':'not a date'}))
    assert policy.reason('29',info('Vert'),['Jaune','Orange','Rouge'],1794229200) is None


def test_failed_persistence_keeps_the_in_process_deduplication():
    db=Mock();db.get_metadata.return_value=None
    policy=VigilanceAlertPolicy(db,Mock(),'#bretagne')
    policy.record_sent('29',info(),1794229200)
    assert policy.reason('29',info(),['Jaune'],1794232800) is None
