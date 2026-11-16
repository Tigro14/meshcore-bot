"""Tests for the airtime command (modules/commands/airtime_command.py).

What these protect is what the reply is allowed to claim: the figure is stated as an
estimate from the modulation, rx and tx stay two numbers, an empty window is not reported
as 0%, the sender line names only this radio — the one sender whose transmissions the
bot can attribute — and states the period it actually covers, and a line that does not
fit in one body is announced as dropped rather than cut mid-name.
"""

import asyncio
import configparser
import json
import pathlib
import types
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from modules.command_manager import CommandManager
from modules.commands import airtime_command
from modules.commands.airtime_command import AirtimeCommand, _render_duration
from tests.conftest import mock_message

CATALOG = json.loads(pathlib.Path('translations/en.json').read_text(encoding='utf-8'))[
    'commands'
]['airtime']

# Channel body for a bot named TestBot: 155 - len('TestBot') - 2, and regional scope
# subtracts 10 more. The reply has to fit in one message, because a second one spends
# airtime on the answer itself.
BODY = 136


def _translate(key, **kwargs):
    """The shipped catalog entry for *key*, so the wording under test is the wording sent."""
    entry = CATALOG[key.rsplit('.', 1)[-1]]
    return entry.format(**kwargs) if kwargs else entry


def _make_bot(bot_name='TestBot'):
    bot = MagicMock()
    bot.logger = Mock()
    config = configparser.ConfigParser()
    config.add_section('Bot')
    config.set('Bot', 'bot_name', bot_name)
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
    bot.db_manager.connection = MagicMock()
    bot.db_manager.connection.return_value.__enter__ = Mock(return_value=Mock())
    bot.db_manager.connection.return_value.__exit__ = Mock(return_value=False)
    # The sender fallback reads the radio's key prefix; a fixed one keeps the reply
    # (and the assertions) about a real prefix instead of about a mock's repr.
    bot.transmission_tracker = types.SimpleNamespace(bot_prefix='40')
    return bot


def _occupancy(*, enabled=True, samples=2, rx=12.3, tx=0.4, seconds=60, network=None):
    """Fake occupancy whose window looks like the real windows() output.

    ``network`` carries the network view; None means a window that counted no
    network at all (the keys are absent), which is what a reply must degrade to.
    """
    occupancy = types.SimpleNamespace()
    occupancy.enabled = enabled
    occupancy.modulation = lambda: {'sf': 9, 'bw_khz': 125.0, 'cr_den': 5, 'preamble': 8}
    occupancy.sample_seconds = 60
    window = {
        'seconds': seconds,
        'rx_percent': rx,
        'tx_percent': tx,
        'samples': samples,
    }
    if network:
        window.update(network)
    occupancy.windows = lambda: {'window': window}
    return occupancy


def _network(talkers=3, dup=1.5, hops=2.0):
    """The network keys a busy window carries, with the defaults a repeat-heavy net shows."""
    return {
        'distinct_senders': talkers,
        'hash_known': 4,
        'unique_packets': round(4 / dup) if dup else 0,
        'dup_factor': dup,
        'hops_known': 2,
        'avg_hops': hops,
    }


def _share(airtime_ms=1052937, period_days=7):
    """The shape ``_bot_radio_share`` hands to the reply: tx over the rolling look-back."""
    return {'airtime_ms': airtime_ms, 'period_days': period_days}


# A fixed clock for every test that reads a period out of stored buckets. The period is
# the ceiling of a day count measured between two clocks — the writer's timestamp and
# this reader's now — so on a real clock the '/2d' a reader sees would move with the
# speed of the interpreter running the test: microseconds of test overhead stay on day
# two, a loaded runner crosses into day three.
_NOW = 1_700_000_000.0


@pytest.fixture(autouse=True)
def _fixed_clock(monkeypatch):
    """Pin the clock ``_bot_radio_share`` reads for every test in this module."""
    monkeypatch.setattr(airtime_command.time, 'time', lambda: _NOW)


class TestReply:
    def setup_method(self):
        self.bot = _make_bot()
        self.cmd = AirtimeCommand(self.bot)

    def test_header_says_the_figure_is_modelled(self):
        """A percentage on a mesh channel reads as a measurement unless it says otherwise.

        Telegraphic wording keeps that disclosure to the one word that carries it:
        'modelled' is the claim, and the budget is bytes of a channel body.
        """
        reply = self.cmd.format_reply(_occupancy(), None, BODY)
        assert 'modelled' in reply
        assert 'SF9' in reply and '125kHz' in reply

    def test_rx_and_tx_stay_two_numbers(self):
        """One node's hearing is not the channel's airtime, so nothing merges them."""
        reply = self.cmd.format_reply(_occupancy(rx=12.3, tx=0.4), None, BODY)
        assert 'rx 12.3%' in reply
        assert 'tx 0.4%' in reply
        assert '12.7' not in reply

    def test_empty_window_is_not_reported_as_zero(self):
        """A quiet window and a recorder that stopped are different facts."""
        reply = self.cmd.format_reply(_occupancy(samples=0), None, BODY)
        assert 'Nothing heard' in reply
        assert '0%' not in reply

    def test_bandwidth_keeps_its_fraction(self):
        """62.5 kHz is not 62 kHz: the header states the modulation the estimate uses."""
        occupancy = _occupancy()
        occupancy.modulation = lambda: {'sf': 8, 'bw_khz': 62.5, 'cr_den': 8, 'preamble': 8}
        reply = self.cmd.format_reply(occupancy, None, BODY)
        assert 'SF8/62.5kHz' in reply

    def test_the_header_names_the_coding_rate_as_the_modulation_uses(self):
        """A coding rate is 4 over a denominator (5..8), not the denominator over 4.

        The header states which modulation the estimate was built from, so putting the
        denominator above the 4 states a modulation that no packet on the channel has ever
        had: CR8/5 is not a coding rate, while the airtime beside it was computed for 4/8.
        The web page renders the same reading as 4/8, so the two must not disagree.
        """
        occupancy = _occupancy()
        occupancy.modulation = lambda: {'sf': 8, 'bw_khz': 62.5, 'cr_den': 8, 'preamble': 8}
        reply = self.cmd.format_reply(occupancy, None, BODY)
        assert 'CR4/8' in reply
        assert 'CR8/5' not in reply

    def test_the_french_catalog_carries_the_reply_strings(self):
        """A bot whose language is fr answers in fr; a command whose strings ship only in en.json
        answers in English through the fallback, so the reader's setting is not what they get."""
        fr = json.loads(pathlib.Path('translations/fr.json').read_text(encoding='utf-8'))['commands']['airtime']
        assert set(CATALOG) <= set(fr)
        # The one telegraphic word that carries the disclosure, in fr as in en.
        assert 'Airtime estimé' in fr['header']
        # The same notation as the English string and as the page: the coding rate is 4 over its
        # denominator, whatever language the reply is written in.
        assert 'CR4/{cr}' in fr['header']

    def test_an_integral_bandwidth_is_not_written_with_a_false_decimal(self):
        """125.0kHz claims a precision the reading does not have; 125kHz does not."""
        reply = self.cmd.format_reply(_occupancy(), None, BODY)
        assert '125kHz' in reply
        assert '125.0kHz' not in reply

    def test_the_sender_line_names_only_this_radio(self):
        """The one sender whose packets the bot can attribute is the radio it drives."""
        reply = self.cmd.format_reply(_occupancy(), _share(airtime_ms=1052937), BODY)
        assert 'this radio: 40 17.5 min/7d' in reply

    def test_the_share_states_the_period_it_covers(self):
        """Retention shorter than the week: the /Nd suffix says what was actually read."""
        reply = self.cmd.format_reply(_occupancy(), _share(airtime_ms=5000, period_days=3), BODY)
        assert '5 s/3d' in reply
        assert '/7d' not in reply

    def test_a_week_without_transmissions_is_a_fact_not_an_omission(self):
        """The bot knows every packet it sent: 0 ms is an observation, not missing evidence."""
        reply = self.cmd.format_reply(_occupancy(), _share(airtime_ms=0), BODY)
        assert 'this radio: 40 0 ms/7d' in reply

    def test_no_share_reads_as_nothing_to_claim(self):
        """No stored buckets (or a failed read) leaves the line out rather than inventing one."""
        reply = self.cmd.format_reply(_occupancy(), None, BODY)
        assert 'this radio' not in reply

    def test_the_share_line_is_dropped_not_cut(self):
        """A line that does not fit is announced as dropped; a cut line reads as another radio.

        The body is measured in bytes — the telegraphic reply leaves the share line
        exactly too big for this one, so the note, not a cut name, is what arrives.
        """
        self.bot.transmission_tracker = types.SimpleNamespace(bot_prefix='verylongprefixwaytoolong')
        reply = self.cmd.format_reply(_occupancy(), _share(), 95)
        assert 'verylongprefix' not in reply
        assert 'dropped' in reply

    def test_names_are_capped_but_never_mid_name(self):
        """A capped name is still a whole shape; the cap is a cut, not an ellipsis."""
        self.bot.transmission_tracker = types.SimpleNamespace(bot_prefix='verylongprefixwaytoolong')
        reply = self.cmd.format_reply(_occupancy(), _share(airtime_ms=214), BODY)
        assert 'verylongpref 214 ms/7d' in reply
        assert 'waytoolong' not in reply
        assert '...' not in reply

    def test_an_absent_stations_count_leaves_no_clause(self):
        """format_reply takes the stations read apart: None is an absent fact, not 0."""
        reply = self.cmd.format_reply(_occupancy(network=_network()), None, BODY, stations=None)
        assert 'net:' in reply
        assert 'station' not in reply


class TestNetworkLine:
    """The network view in the reply: what this node heard, stated as a view.

    The same honesty rules as the airtime line: a count with no evidence behind it
    is left out rather than rendered as 0 or x1, and when the body runs out of room
    the network line — part of the answer — is never dropped silently.
    """

    def setup_method(self):
        self.bot = _make_bot()
        self.cmd = AirtimeCommand(self.bot)

    def _reply(self, occupancy, limit=BODY, stations=None, share=None):
        return self.cmd.format_reply(occupancy, share, limit, stations)

    def test_the_window_says_who_and_what_was_heard(self):
        reply = self._reply(_occupancy(network=_network()), limit=400, stations=5)
        assert 'net:' in reply
        assert '3 talkers' in reply
        assert 'repeats x1.5' in reply
        assert 'avg 2 hops' in reply
        assert '5 stations/24h' in reply

    def test_an_unknown_repeat_factor_is_left_out_not_rendered_as_x1(self):
        """'repeats x1' with no hashed packet would claim a network without repeats."""
        network = _network(dup=None, hops=None)
        network['hash_known'] = 0
        network['unique_packets'] = 0
        network['hops_known'] = 0
        reply = self._reply(_occupancy(network=network))
        assert '3 talkers' in reply
        assert 'repeats' not in reply
        assert 'hops' not in reply

    def test_a_direct_hear_is_stated_not_dropped(self):
        """avg_hops 0.0 is an answer — everything heard direct — not a missing figure."""
        reply = self._reply(_occupancy(network=_network(talkers=1, dup=1.0, hops=0.0)))
        assert 'avg 0 hops' in reply
        assert 'repeats x1' in reply

    def test_the_window_says_where_the_unnamed_copies_went(self):
        """2 packets and 1 talker reconcile only when the reader sees the rest.

        The unnamed copies and the emitter addresses are counts of their own: the
        first reconciles the packet total against the talkers, the second answers
        how many addresses the flood path proved carried these copies — a question
        the talkers count never answered, whatever it is labelled in the catalogue.
        """
        network = _network(talkers=1)
        network['unattributed_packets'] = 1
        network['emitter_labels'] = 2
        reply = self._reply(_occupancy(network=network), limit=400)
        assert '1 talker,' in reply
        assert '1 unnamed copy,' in reply
        assert '2 emitter addresses' in reply

    def test_a_count_of_one_reads_as_one(self):
        """'1 talkers' is a number the reader has to re-read.

        The plural form is the catalogue's default and the singular is
        ``<key>_one``; a language that ships no singular keeps the plural rather
        than printing a key in the reply.
        """
        network = _network(talkers=1)
        network['unattributed_packets'] = 1
        network['emitter_labels'] = 1
        reply = self._reply(_occupancy(network=network), limit=400)
        assert '1 talkers' not in reply
        assert '1 unnamed copies' not in reply
        assert '1 emitter addresses' not in reply
        # Two of anything still reads as two.
        network['distinct_senders'] = 2
        plural = self._reply(_occupancy(network=network), limit=400)
        assert '2 talkers' in plural
        assert '1 unnamed copy,' in plural

    def test_the_french_reply_agrees_with_its_own_numbers(self):
        """The fr reply is French, so its clauses agree in number and gender.

        The shipped catalogue is what gets sent, so this checks the real strings:
        one talker is `1 expéditeur identifié`, one unnamed copy is
        `1 copie sans identité`, one address is `1 adresse émettrice`.
        """
        fr = json.loads(pathlib.Path('translations/fr.json').read_text(encoding='utf-8'))[
            'commands'
        ]['airtime']
        assert fr['network_talkers_one'].format(n=1) == '1 expéditeur identifié'
        assert fr['network_unattributed_one'].format(n=1) == '1 copie sans identité'
        assert fr['network_emitters_one'].format(n=1) == '1 adresse émettrice'
        # The plural stays the plural: '1' is the only case that takes the other form.
        assert fr['network_talkers'].format(n=2) == '2 expéditeurs identifiés'
        assert fr['network_emitters'].format(n=0) == '0 adresses émettrices'

    def test_the_new_clauses_are_absent_when_the_window_holds_no_such_count(self):
        """An absent count is not a zero count: no clause, not '0 unnamed copies'."""
        network = _network()
        network['unattributed_packets'] = 0
        network['emitter_labels'] = 0
        reply = self._reply(_occupancy(network=network), limit=400)
        assert 'unnamed' not in reply
        assert 'emitter addresses' not in reply

    def test_a_window_without_network_keys_still_replies_the_airtime(self):
        """A window that counted no network is not a reply without an answer."""
        reply = self._reply(_occupancy())
        assert 'net:' not in reply
        assert 'rx 12.3%' in reply

    def test_no_samples_means_no_network_to_count(self):
        reply = self._reply(_occupancy(samples=0, network=_network()), stations=5)
        assert 'Nothing heard' in reply
        assert 'net:' not in reply
        assert 'station' not in reply

    def test_the_stations_clause_gives_up_before_the_radio_share(self):
        """Body out of room: the least specific number goes first, and both variants
        still state what they count — no ellipsis, no silently shortened claim."""
        occupancy = _occupancy(network=_network())
        share = _share(airtime_ms=214)
        full = self._reply(occupancy, 400, stations=5, share=share)
        assert '5 stations/24h' in full and len(full) > BODY

        # A body too small for the stations clause but big enough for the share line:
        # the clause goes, the share stays whole. The comparison is the same byte
        # comparison the ladder and the channel make — the telegraphic reply is all
        # ASCII in en, but the fr one is not, and the ladder must not change its
        # answer with the language.
        tight = self._reply(occupancy, BODY, stations=5, share=share)
        assert len(tight.encode('utf-8')) <= BODY
        assert '5 stations' not in tight
        assert 'net:' in tight and 'repeats x1.5' in tight
        assert '40 214 ms/7d' in tight
        assert 'dropped' not in tight

    def test_the_network_line_is_never_dropped_silently(self):
        """If core+network cannot fit, the reply is returned whole for the splitter;
        quietly losing the network line would read as a calm network."""
        occupancy = _occupancy(network=_network())
        share = _share(airtime_ms=214)
        reply = self._reply(occupancy, 120, stations=5, share=share)
        assert len(reply) > 120
        assert 'net:' in reply
        assert 'repeats x1.5' in reply

    def test_the_stations_count_reaches_the_reply_through_execute(self):
        """execute() reads the density itself; a reply that never calls it has no clause."""
        message = mock_message(content='airtime')
        self.bot.occupancy = _occupancy(network=_network())
        # One shared fetchone mock answers both reads execute() makes: the radio share
        # first (SUM, MIN bucket_ts), then the stations count.
        self.bot.db_manager.connection.return_value.__enter__.return_value.execute.return_value.fetchone.side_effect = [
            (214.0, _NOW - 2 * 86400),
            (7,),
        ]
        sent = []
        self.bot.command_manager.send_response = AsyncMock(
            side_effect=lambda msg, content, **kw: sent.append(content) or True
        )
        self.bot.command_manager.send_response_chunked = AsyncMock(
            side_effect=lambda msg, chunks, **kw: sent.extend(chunks) or True
        )
        ok = asyncio.run(self.cmd.execute(message))
        assert ok is True
        # Whether one body or split, the density clause is in the answer — the splitter
        # breaks at line boundaries, so the clause arrives whole.
        assert '7 stations/24h' in '\n'.join(sent)

    def test_a_failed_stations_read_omits_the_clause_not_a_zero(self):
        """An absent table or a failed read is a missing fact; '0 stations' claims an empty area."""
        self.bot.db_manager.connection.return_value.__enter__.return_value.execute.side_effect = Exception('no such table')
        assert self.cmd._heard_stations() is None
        reply = self.cmd.format_reply(_occupancy(network=_network()), None, BODY, self.cmd._heard_stations())
        assert 'net:' in reply
        assert 'station' not in reply


class TestRadioShare:
    """This radio's own share: tx summed over the rolling look-back, named with descending certainty.

    ``_bot_radio_share`` reads the stored buckets, so the oldest row it saw decides what
    the period suffix may claim; ``_radio_name`` resolves the advertised name, then the
    contact record for the radio's full key, then the raw prefix — and a short prefix is
    never looked up in contacts, because a 1-2 byte prefix names a crowd of strangers as
    readily as it names this radio, and a wrong name is worse than an opaque one.
    """

    def setup_method(self):
        self.bot = _make_bot()
        self.cmd = AirtimeCommand(self.bot)

    def _fetchone(self, row):
        self.bot.db_manager.connection.return_value.__enter__.return_value.execute.return_value.fetchone.return_value = row

    def test_the_share_sums_tx_over_the_look_back(self):
        self._fetchone((1052937.0, _NOW - 20 * 86400))
        share = self.cmd._bot_radio_share(_occupancy())
        assert share == {'airtime_ms': 1052937.0, 'period_days': 7}

    def test_a_shorter_history_says_what_it_covers(self):
        """Retention under a week, or a fresh install: /7d would claim days never read."""
        self._fetchone((5000.0, _NOW - 3 * 86400 - 120))
        share = self.cmd._bot_radio_share(_occupancy())
        assert share['period_days'] == 4

    def test_an_exact_boundary_is_not_rounded_up_into_an_unclaimed_day(self):
        """A bucket two days old covers two days; the two clocks need not agree exactly.

        bucket_ts is the writer's reading and ``now`` this reader's, on clocks that
        drift apart by seconds. 30 s of disagreement is a third of a minute, not a
        third of a day, so it must not promote the claim to /3d.
        """
        self._fetchone((5000.0, _NOW - 2 * 86400 - 30))
        share = self.cmd._bot_radio_share(_occupancy())
        assert share['period_days'] == 2

    def test_a_week_without_transmissions_is_a_zero_sum_not_an_absence(self):
        self._fetchone((0.0, _NOW - 6 * 86400))
        share = self.cmd._bot_radio_share(_occupancy())
        assert share == {'airtime_ms': 0.0, 'period_days': 6}

    def test_no_rows_leave_nothing_to_claim(self):
        self._fetchone((None, None))
        assert self.cmd._bot_radio_share(_occupancy()) is None

    def test_a_failed_read_is_not_reported_as_a_zero(self):
        """A missing table is a missing fact; '0 ms/7d' would claim a silent radio."""
        self.bot.db_manager.connection.return_value.__enter__.return_value.execute.side_effect = Exception(
            'no such table'
        )
        assert self.cmd._bot_radio_share(_occupancy()) is None

    def test_a_row_that_is_not_the_two_columns_asked_for_reads_as_no_fact(self):
        """Interpreting the row is part of the read: a malformed row is a missing fact,
        not a crash that turns the whole reply into an error message."""
        self._fetchone((7,))
        assert self.cmd._bot_radio_share(_occupancy()) is None

    def test_the_radio_name_prefers_the_advertised_name(self):
        self.bot.meshcore = types.SimpleNamespace(self_info={'name': 'TigroBot'})
        assert self.cmd._radio_name() == 'TigroBot'

    def test_the_radio_name_falls_back_to_the_contact_record(self):
        self.bot.meshcore = types.SimpleNamespace(self_info={'name': '', 'public_key': 'ABCD1234'})
        self._fetchone(('TigroBot',))
        assert self.cmd._radio_name() == 'TigroBot'

    def test_the_radio_name_never_buys_a_prefix_through_contacts(self):
        self.bot.meshcore = types.SimpleNamespace(self_info={'public_key': 'ABCD'})
        self._fetchone(None)
        assert self.cmd._radio_name() == '40'

    def test_durations_read_in_the_unit_their_size_deserves(self):
        """1052937ms is the number nobody reads at a glance; 17.5 min is."""
        assert _render_duration(900) == '900 ms'
        assert _render_duration(12500) == '12.5 s'
        assert _render_duration(1052937) == '17.5 min'
        assert _render_duration(7200000) == '2 h'


class TestExecution:
    def setup_method(self):
        self.bot = _make_bot()
        self.cmd = AirtimeCommand(self.bot)

    def _run(self, occupancy, message):
        self.bot.occupancy = occupancy
        sent = []
        self.bot.command_manager.send_response = AsyncMock(
            side_effect=lambda msg, content, **kw: sent.append(content) or True
        )
        self.bot.command_manager.send_response_chunked = AsyncMock(
            side_effect=lambda msg, chunks, **kw: sent.extend(chunks) or True
        )
        ok = asyncio.run(self.cmd.execute(message))
        return ok, sent

    def test_recording_off_explains_itself(self):
        """Off by default, so the reply cannot answer 0% for a feature that never ran."""
        ok, sent = self._run(_occupancy(enabled=False), mock_message(content='airtime'))
        assert ok is False
        assert '[Channel_Occupancy]' in sent[0]
        assert '%' not in sent[0]

    def test_missing_occupancy_says_so(self):
        ok, sent = self._run(None, mock_message(content='airtime'))
        assert ok is False
        assert 'not available' in sent[0]

    def test_disabled_command_says_so(self):
        self.bot.config.set('Airtime_Command', 'enabled', 'false')
        self.cmd.command_enabled = False
        ok, sent = self._run(_occupancy(), mock_message(content='airtime'))
        assert ok is False
        assert '[Airtime_Command]' in sent[0]

    def test_executes_and_replies_one_body(self):
        message = mock_message(content='airtime')
        self.bot.db_manager.connection.return_value.__enter__.return_value.execute.return_value.fetchone.return_value = (
            214.0, _NOW - 2 * 86400,
        )
        ok, sent = self._run(_occupancy(), message)
        assert ok is True
        assert len(sent) == 1
        assert len(sent[0]) <= self.cmd.get_max_message_length(message)
        assert '40 214 ms/2d' in sent[0]

    def test_reply_that_does_not_fit_is_split_not_cut(self):
        """A long bot name leaves a body too small for the reply, so it travels in pieces.

        The pieces break at line boundaries, so the sender name arrives whole in one
        message. Cutting at the byte limit would deliver 'verylongpre' as if it were a
        different radio, and the reader would have no way to tell.
        """
        self.bot.config.set('Bot', 'bot_name', 'TestBotWithAVeryLongMeshUserNameForBudget')
        self.cmd = AirtimeCommand(self.bot)
        self.bot.transmission_tracker = types.SimpleNamespace(bot_prefix='verylongprefixwaytoolong')
        share = _share(airtime_ms=214)
        self.bot.db_manager.connection.return_value.__enter__.return_value.execute.return_value.fetchone.return_value = (
            214.0, _NOW - 2 * 86400,
        )
        # A window with a network line as well: the telegraphic reply got short enough
        # that the core alone would fit even this cramped body, and the point of the
        # test is a reply that genuinely overflows one.
        occupancy = _occupancy(network=_network())
        message = mock_message(content='airtime')
        limit = self.cmd.get_max_message_length(message)
        full = self.cmd.format_reply(occupancy, share, 400)
        assert limit < len(full), "the body has to be too small for the reply at all"

        ok, sent = self._run(occupancy, message)
        assert ok is True
        assert len(sent) > 1
        assert all(len(part) <= limit for part in sent)
        # Each capped name is whole inside one piece, not shared between two. The parts
        # are printed on failure because they are the evidence: 'assert False' would
        # describe the symptom and hide which line moved.
        assert any('verylongpref 214 ms/2d' in part for part in sent), sent
