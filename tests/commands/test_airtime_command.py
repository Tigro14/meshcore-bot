"""Tests for the airtime command (modules/commands/airtime_command.py).

What these protect is what the reply is allowed to claim: it answers exactly three
figures — the day's peak and the hour it sat, the last-hour average and the day
average — and nothing
else; the figures are stated as an estimate from the modulation; the peak's hour is
the worst bucket's own clock, rendered in the operator's configured timezone; averages
divide by
the capacity elapsed, not by the buckets that happen to exist, so silence prices as
silence; a day with no sample recorded says so rather than answering 0 %; and a
share above 100 % is reported as heard, because parallel transmissions each cost
their airtime.
"""

import asyncio
import configparser
import json
import pathlib
import string
import types
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from modules.command_manager import CommandManager
from modules.commands import airtime_command
from modules.commands.airtime_command import AirtimeCommand, _render_g
from tests.conftest import mock_message

CATALOG = json.loads(pathlib.Path('translations/en.json').read_text(encoding='utf-8'))[
    'commands'
]['airtime']

# Channel body for a bot named TestBot: 155 - len('TestBot') - 2. The reply has to
# fit in one message, because a second one spends airtime on the answer itself.
BODY = 136

# A fixed clock for every test that reads a period out of stored buckets. The day is
# the ceiling of a day count measured from the epoch, and the hour window is cut from
# this reader's now — on a real clock the boundary a test asserts against would move
# with the speed of the interpreter running it.
_NOW = 1_700_000_000.0
_DAY_START = 1_699_920_000  # _NOW // 86400 * 86400


def _translate(key, **kwargs):
    """The shipped catalog entry for *key*, so the wording under test is the wording sent."""
    entry = CATALOG[key.rsplit('.', 1)[-1]]
    return entry.format(**kwargs) if kwargs else entry


def _make_bot(bot_name='TestBot', *, row=None, peak_row=None, tz='', raise_on_read=False):
    """Bot whose stored-buckets read returns *row* and whose peak-bucket read returns *peak_row*.

    ``row`` is the fetchone() shape the aggregate read asks for:
    ``(count, day_rx_sum_ms, day_peak_ms, hour_rx_sum_ms)``.
    ``peak_row`` is the worst bucket's ``(bucket_ts,)`` from the second read.
    """
    bot = types.SimpleNamespace()
    bot.logger = Mock()
    bot.occupancy = None  # set by the tests that want a recorder in the process
    config = configparser.ConfigParser()
    config.add_section('Bot')
    config.set('Bot', 'bot_name', bot_name)
    config.set('Bot', 'timezone', tz)
    config.add_section('Channels')
    config.set('Channels', 'monitor_channels', 'general')
    config.set('Channels', 'respond_to_dms', 'true')
    config.add_section('Keywords')
    config.add_section('Airtime_Command')
    config.set('Airtime_Command', 'enabled', 'true')
    bot.config = config
    bot.meshcore = None  # no device: the body budget falls back to bot_name
    bot.translator = MagicMock()
    bot.translator.translate = Mock(side_effect=_translate)
    bot.command_manager = MagicMock()
    bot.command_manager.monitor_channels = ['general']
    # The real splitter, so the test measures how the reply is broken up in practice
    # rather than how a stand-in happens to slice it.
    bot.command_manager.split_text_into_utf8_chunks = Mock(
        side_effect=lambda text, max_bytes: CommandManager.split_text_into_utf8_chunks(
            text, max_bytes
        )
    )

    aggregate = Mock()
    aggregate.execute = Mock(return_value=aggregate)
    aggregate.fetchone = Mock(return_value=row)
    bucket = Mock()
    bucket.execute = Mock(return_value=bucket)
    bucket.fetchone = Mock(return_value=peak_row)

    def _reads(*_args, **_kwargs):
        # The two reads the command makes: the day aggregate first, then the peak
        # bucket's clock. A Mock returning one shape for both would hide a wrong or
        # missing second read. A callable, not a two-item list: a list runs dry once
        # and a later read would then invent a Mock-shaped fact. Each read ends in
        # .fetchone(), so hand back the cursor itself. Count first: this callable
        # runs as the side_effect, so the call being served is already number 1.
        target = aggregate if _reads.calls < 1 else bucket
        _reads.calls += 1
        return target

    _reads.calls = 0
    reader = Mock(side_effect=_reads)

    bot.db_manager = MagicMock()
    # `with db_manager.connection() as conn` runs __enter__ on connection() itself
    # (and hands back that same object every time), so the execute the command sees
    # lives on connection() — not on connection.return_value.
    conn_cm = bot.db_manager.connection()
    conn_cm.execute = reader
    # The `with` runs __enter__ on connection() and uses its return as conn: pin it
    # to connection() itself, so the execute above is the one the command sees.
    conn_cm.__enter__ = Mock(return_value=conn_cm)
    conn_cm.__exit__ = Mock(return_value=False)
    bot.reads = reader  # tests read its call_args_list
    if raise_on_read:
        conn_cm.execute = Mock(side_effect=OSError('no such table: occupancy_samples'))
    return bot


def _occupancy(*, enabled=True, sample_seconds=60):
    occupancy = types.SimpleNamespace()
    occupancy.enabled = enabled
    occupancy.sample_seconds = sample_seconds
    return occupancy


@pytest.fixture(autouse=True)
def _fixed_clock(monkeypatch):
    """Pin the clock ``_day_stats`` reads for every test in this module."""
    monkeypatch.setattr(airtime_command.time, 'time', lambda: _NOW)


class TestDayStats:
    def setup_method(self):
        # 60 s buckets: a 45 000 ms peak bucket is 75 % of one bucket's capacity;
        # 3 600 000 ms over the last hour is 100 % of that hour; 108 000 000 ms over
        # the 80 000 s elapsed of this UTC day is 135 % — deliberately above 100 to
        # pin that the day average is not clamped. The peak bucket sat one hour into
        # the UTC day.
        self.peak_ts = _DAY_START + 3600
        self.bot = _make_bot(
            row=(42, 108_000_000.0, 45_000.0, 3_600_000.0), peak_row=(self.peak_ts,)
        )
        self.cmd = AirtimeCommand(self.bot)

    def test_peak_is_the_worst_bucket_at_its_own_capacity(self):
        stats = self.cmd._day_stats(_occupancy())
        assert stats['peak_percent'] == pytest.approx(75.0)

    def test_hour_average_divides_by_the_hour_not_by_the_busy_buckets(self):
        """One full hour of rx over the last hour reads as 100 %, whatever the bucket count."""
        stats = self.cmd._day_stats(_occupancy())
        assert stats['hour_percent'] == pytest.approx(100.0)

    def test_day_average_divides_by_the_elapsed_day(self):
        stats = self.cmd._day_stats(_occupancy())
        elapsed_ms = (_NOW - _DAY_START) * 1000
        assert stats['day_percent'] == pytest.approx(108_000_000.0 / elapsed_ms * 100)

    def test_above_hundred_is_kept_not_clamped(self):
        """Parallel transmissions each cost their airtime; a merged share can pass 100 %."""
        stats = self.cmd._day_stats(_occupancy())
        assert stats['day_percent'] > 100.0

    def test_read_windows_are_the_utc_day_and_the_trailing_hour(self):
        """The day is the UTC day the buckets are aligned on; the hour window is exactly 1 h."""
        self.cmd._day_stats(_occupancy(sample_seconds=60))
        sql, args = self.bot.reads.call_args_list[0].args
        assert args == (int(_NOW - 3600), 60, _DAY_START)
        assert 'bucket_ts >= ?' in sql

    def test_peak_time_is_the_worst_buckets_own_clock_on_the_same_day(self):
        """A peak the reader cannot go and look at is half a fact; the hour is read
        from the same day window, so it can never point outside the day the peak answers for."""
        stats = self.cmd._day_stats(_occupancy(sample_seconds=60))
        assert stats['peak_ts'] == self.peak_ts
        sql, args = self.bot.reads.call_args_list[1].args
        assert args == (60, _DAY_START)
        assert 'ORDER BY rx_ms DESC LIMIT 1' in sql

    def test_a_day_without_a_peak_timestamp_keeps_the_hour_unknown(self):
        """An unknown hour is absent from the stats, not a midnight invented by the reader."""
        bot = _make_bot(row=(42, 108_000_000.0, 45_000.0, 3_600_000.0), peak_row=None)
        assert AirtimeCommand(bot)._day_stats(_occupancy())['peak_ts'] is None

    def test_peak_prices_the_recorders_bucket_size_not_a_guessed_one(self):
        """A recorder on 30 s buckets has half the capacity per bucket; the peak has to follow."""
        bot = _make_bot(row=(1, 1_000.0, 15_000.0, 1_000.0))
        stats = AirtimeCommand(bot)._day_stats(_occupancy(sample_seconds=30))
        assert stats['peak_percent'] == pytest.approx(50.0)

    def test_no_bucket_today_reads_as_no_fact_not_as_zero(self):
        for row in (None, (0, None, None, None)):
            bot = _make_bot(row=row)
            assert AirtimeCommand(bot)._day_stats(_occupancy()) is None

    def test_failed_read_is_raised_not_reported_as_a_quiet_day(self):
        """'No samples today' and 'the read failed' must stay two different answers."""
        bot = _make_bot(raise_on_read=True)
        with pytest.raises(OSError):
            AirtimeCommand(bot)._day_stats(_occupancy())


class TestReply:
    def setup_method(self):
        self.peak_ts = _DAY_START + 3600  # 01:00 UTC, which is 02:00 in Europe/Paris
        self.bot = _make_bot(
            row=(42, 108_000_000.0, 45_000.0, 3_600_000.0),
            peak_row=(self.peak_ts,),
            tz='Europe/Paris',
        )
        self.cmd = AirtimeCommand(self.bot)

    def test_reply_says_the_figure_is_an_estimate(self):
        """A percentage on a mesh channel reads as a measurement unless it says otherwise."""
        reply = self.cmd.format_reply(self.cmd._day_stats(_occupancy()), BODY)
        assert 'Estimated' in reply

    def test_reply_carries_exactly_the_three_figures_and_the_peak_hour(self):
        stats = self.cmd._day_stats(_occupancy())
        reply = self.cmd.format_reply(stats, BODY)
        assert f"peak: {_render_g(stats['peak_percent'])}%" in reply
        assert f"1 h average: {_render_g(stats['hour_percent'])}%" in reply
        assert f"day average: {_render_g(stats['day_percent'])}%" in reply
        assert "at 02:00" in reply  # the operator's configured timezone, not a bare UTC hour
        for word in ('talker', 'sender', 'SF', 'hops', 'stations'):
            assert word not in reply

    def test_no_stats_answers_there_is_nothing_recorded(self):
        reply = self.cmd.format_reply(None, BODY)
        assert 'No airtime samples recorded today' in reply
        assert '0' not in reply

    def test_reply_fits_in_one_channel_body(self):
        reply = self.cmd.format_reply(self.cmd._day_stats(_occupancy()), BODY)
        assert len(reply.encode('utf-8')) <= BODY


class TestRendering:
    def test_rounding_drops_false_precision(self):
        assert _render_g(round(12.04, 1)) == '12'
        assert _render_g(round(2.44, 1)) == '2.4'
        assert _render_g(round(0.04, 1)) == '0'

    def test_no_invented_second_decimal(self):
        """The reply budget is bytes of a channel body; one decimal is what it can defend."""
        stats = {'peak_percent': 75.0, 'hour_percent': 33.333, 'day_percent': 0.0, 'peak_ts': _DAY_START + 3600}
        reply = AirtimeCommand(_make_bot(tz='UTC')).format_reply(stats, BODY)
        assert '33.3%' in reply
        assert '.' not in reply.split('day average:')[1].split('%')[0]
        assert 'at 01:00' in reply  # the hour is not rounded or re-priced by the percentage path


class TestExecute:
    def setup_method(self):
        self.bot = _make_bot(
            row=(42, 108_000_000.0, 45_000.0, 3_600_000.0),
            peak_row=(_DAY_START + 3600,),
            tz='UTC',
        )
        self.cmd = AirtimeCommand(self.bot)
        self.sent = []
        self.bot.command_manager.send_response = AsyncMock(
            side_effect=lambda msg, content, **kw: self.sent.append(content) or True
        )
        self.bot.command_manager.send_response_chunked = AsyncMock(
            side_effect=lambda msg, chunks, **kw: self.sent.extend(chunks) or True
        )

    def _run(self, message_text='airtime'):
        return asyncio.run(self.cmd.execute(mock_message(message_text)))

    def test_disabled_command_says_how_to_enable_it(self):
        self.bot.config.set('Airtime_Command', 'enabled', 'false')
        cmd = AirtimeCommand(self.bot)
        self.bot.occupancy = _occupancy()
        assert asyncio.run(cmd.execute(mock_message('airtime'))) is False
        assert 'disabled' in self.sent[0] and '[Airtime_Command]' in self.sent[0]

    def test_no_recorder_in_this_process_says_so(self):
        self.bot.occupancy = None
        assert self._run() is False
        assert 'unavailable' in self.sent[0].lower()

    def test_recording_off_says_how_to_start_it_not_zero(self):
        """Off by default: answering 0 % for a recorder that never ran invents a calm channel."""
        self.bot.occupancy = _occupancy(enabled=False)
        assert self._run() is False
        assert '[Channel_Occupancy]' in self.sent[0]
        assert '0' not in self.sent[0]

    def test_no_bucket_today_answers_the_nothing_recorded_line(self):
        def ex(_sql, _params):
            # The day aggregate reports zero buckets; the peak-bucket read then
            # finds nothing to point at.
            r = Mock()
            r.fetchone.return_value = (0, None, None, None)
            return r

        self.bot.db_manager.connection().execute = ex
        self.bot.occupancy = _occupancy()
        assert self._run() is True
        assert 'No airtime samples recorded today' in self.sent[0]

    def test_three_figures_reach_the_sender_whole(self):
        self.bot.occupancy = _occupancy()
        assert self._run() is True
        assert 'day peak' in self.sent[0] and '1 h average' in self.sent[0] and 'day average' in self.sent[0]
        assert 'at 01:00' in self.sent[0]

    def test_a_body_too_cramped_is_split_not_cut(self):
        """Regional scope and a long sender name shrink the body; the answer still arrives whole."""
        self.bot.occupancy = _occupancy()
        message = mock_message('airtime')
        message.is_dm = False
        self.bot.meshcore = None
        cramped = 40
        monkey_len = Mock(return_value=cramped)
        self.cmd.get_max_message_length = monkey_len
        assert asyncio.run(self.cmd.execute(message)) is True
        assert len(self.sent) > 1
        assert ''.join(self.sent).count('average') == 2  # nothing lost between chunks

    def test_error_path_reports_the_failure(self):
        self.bot.occupancy = _occupancy()
        self.bot.db_manager.connection.side_effect = OSError('database is locked')
        assert self._run() is False
        assert 'read error' in self.sent[0].lower()


class TestCommandCatalog:
    def test_reply_and_no_samples_exist_in_both_languages_with_the_same_placeholders(self):
        fr = json.loads(pathlib.Path('translations/fr.json').read_text(encoding='utf-8'))[
            'commands'
        ]['airtime']
        assert set(fr) == set(CATALOG)
        for key in ('reply', 'no_samples', 'disabled', 'unavailable', 'recording_off', 'error'):
            assert key in fr and fr[key]
        for key in CATALOG:
            fr_ph = {f[1] for f in string.Formatter().parse(fr[key]) if f[1] is not None}
            en_ph = {f[1] for f in string.Formatter().parse(CATALOG[key]) if f[1] is not None}
            assert fr_ph == en_ph, key

    def test_the_old_network_and_sender_keys_are_gone(self):
        """Nothing else in the project reads them; keeping them would let a future reply
        resurrect detail the command is meant to leave on the page."""
        gone = {'header', 'window', 'network_prefix', 'radio_share', 'top_senders_omitted'}
        assert not (gone & set(CATALOG))
