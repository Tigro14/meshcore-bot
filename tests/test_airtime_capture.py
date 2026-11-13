"""Healthy silence requires continuity evidence, never packet interpolation."""
import logging
import queue
import sqlite3
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from modules.airtime_analysis import build_report
from modules.airtime_analysis import create_tables as create_analysis
from modules.airtime_capture import ListeningRecorder, covered_buckets, create_tables, listening_intervals
from modules.airtime_quality import reception_history


@pytest.fixture
def conn():
    db = sqlite3.connect(':memory:')
    create_tables(db.cursor())
    yield db
    db.close()


def test_recorder_requires_two_healthy_ticks_and_does_not_fill_restart_gap(conn):
    recorder = ListeningRecorder()
    recorder.record(conn, 1000, 10, True)
    assert listening_intervals(conn, 0, 2000) == []
    recorder.record(conn, 1030, 40, True)
    recorder.record(conn, 1060, 70, False)
    recorder.record(conn, 1090, 100, True)
    recorder.record(conn, 1120, 130, True)
    ListeningRecorder().record(conn, 1150, 160, True)
    assert listening_intervals(conn, 0, 2000) == [(1000, 1030), (1090, 1120)]


@pytest.mark.parametrize('wall,mono,loss', [(1100, 110, 0), (1040, 40, 0), (1030, 40, 1010)])
def test_stall_wall_clock_jump_or_capture_loss_breaks_continuity(conn, wall, mono, loss):
    recorder = ListeningRecorder()
    recorder.record(conn, 1000, 10, True)
    recorder.record(conn, wall, mono, True, loss)
    assert listening_intervals(conn, 0, 2000) == []


def test_overlapping_intervals_cannot_hide_an_unknown_gap():
    assert covered_buckets([(0, 150), (0, 150), (200, 300)], 0, 300, 300) == [250]
    series = reception_history([], 0, 900, [(0, 150), (150, 300), (600, 899)])
    assert [point['percent'] for point in series['rx_series']] == [0]*5 + [None]*5 + [0]*4 + [None]
    assert [series['rx_series'][index]['coverage'] for index in (0,5,14)] == ['complete', 'unknown', 'partial']
    assert series['rx_confirmed_listening_seconds'] == 599
    assert series['rx_peak_five_minute_percent'] == 0
    assert reception_history([], 0, 900)['rx_peak_five_minute_percent'] is None


def test_rx_is_retained_when_capture_coverage_is_incomplete():
    result = reception_history([(100, 3000, True)], 0, 300, [(90, 120)])
    assert result['rx_series'][1]['percent'] == 5
    assert result['rx_series'][1]['coverage'] == 'partial'
    assert result['rx_series'][1]['listening_seconds'] == 30


def test_empty_capture_is_zero_only_if_the_entire_requested_window_is_confirmed(conn):
    create_analysis(conn.cursor())
    conn.execute('INSERT INTO airtime_capture_intervals VALUES (0,3600)')
    report = build_report(conn, now=3600, window='1h')
    assert report['local']['rx_percent'] == 0
    assert all(point['percent'] == 0 for point in report['local']['rx_series'])
    conn.execute('DELETE FROM airtime_capture_intervals')
    conn.execute('INSERT INTO airtime_capture_intervals VALUES (0,300)')
    assert build_report(conn, now=3600, window='1h')['local']['rx_percent'] is None


def test_capture_retention_is_bounded_without_touching_observations(conn):
    conn.execute('INSERT INTO airtime_capture_intervals VALUES (0,30)')
    recorder = ListeningRecorder()
    recorder.record(conn, 31*86400, 10, True)
    assert conn.execute('SELECT COUNT(*) FROM airtime_capture_intervals').fetchone()[0] == 0


def test_idle_writer_records_without_packets_or_radio_calls(tmp_path, monkeypatch):
    import modules.web_viewer.integration as module
    from modules.web_viewer.integration import BotIntegration

    path = tmp_path/'capture.db'
    with sqlite3.connect(path) as db:
        create_tables(db.cursor())
    integration = object.__new__(BotIntegration)
    integration.bot = SimpleNamespace(occupancy=SimpleNamespace(enabled=True), connected=True,
        meshcore=SimpleNamespace(is_connected=True), is_radio_zombie=False, _last_health_update=1000,
        connection_time=900, logger=logging.getLogger('test'))
    integration.is_shutting_down = False
    integration._write_queue = queue.Queue()
    integration._get_web_viewer_db_path = Mock(return_value=path)
    now = [1000]
    monkeypatch.setattr(module.time, 'time', lambda: now[0])
    monkeypatch.setattr(module.time, 'monotonic', lambda: now[0])
    integration._record_listening_health()
    now[0] = 1030
    integration._record_listening_health()
    # A disconnect/reconnect between ticks must not be painted as continuous RX.
    integration.bot.connection_time = 1040
    now[0] = 1060
    integration._record_listening_health()
    now[0] = 1090  # Existing main-loop heartbeat is now stale.
    integration._record_listening_health()
    with sqlite3.connect(path) as db:
        assert listening_intervals(db, 0, 2000) == [(1000, 1030)]
    assert integration._write_queue.empty()
