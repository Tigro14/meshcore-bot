"""Tests for the airtime command (modules/commands/airtime_command.py).

What these protect is what the reply is allowed to claim: the figure is stated as an
estimate from the modulation, rx and tx stay two numbers, an empty window is not reported
as 0%, and a sender list that does not fit in one body is announced as dropped rather than
cut mid-name, which would read as a different node.
"""

import asyncio
import configparser
import json
import pathlib
import types
from unittest.mock import AsyncMock, MagicMock, Mock

from modules.command_manager import CommandManager
from modules.commands.airtime_command import AirtimeCommand
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
    return bot


def _occupancy(*, enabled=True, samples=2, rx=12.3, tx=0.4, seconds=60, senders=None):
    occupancy = types.SimpleNamespace()
    occupancy.enabled = enabled
    occupancy.modulation = lambda: {'sf': 9, 'bw_khz': 125.0, 'cr_den': 5, 'preamble': 8}
    occupancy.windows = lambda: {
        'window': {
            'seconds': seconds,
            'rx_percent': rx,
            'tx_percent': tx,
            'samples': samples,
        }
    }
    senders = senders or []
    occupancy.top_senders = lambda conn, limit: senders[:limit]
    return occupancy


def _sender(name, airtime_ms, packets):
    return {'sender': name, 'airtime_ms': airtime_ms, 'packets': packets}


class TestReply:
    def setup_method(self):
        self.bot = _make_bot()
        self.cmd = AirtimeCommand(self.bot)

    def test_header_says_the_figure_is_modelled(self):
        """A percentage on a mesh channel reads as a measurement unless it says otherwise."""
        reply = self.cmd.format_reply(_occupancy(), [], BODY)
        assert 'modelled' in reply
        assert 'not measured' in reply
        assert 'SF9' in reply and '125kHz' in reply

    def test_rx_and_tx_stay_two_numbers(self):
        """One node's hearing is not the channel's airtime, so nothing merges them."""
        reply = self.cmd.format_reply(_occupancy(rx=12.3, tx=0.4), [], BODY)
        assert 'rx 12.3%' in reply
        assert 'tx 0.4%' in reply
        assert '12.7' not in reply

    def test_empty_window_is_not_reported_as_zero(self):
        """A quiet window and a recorder that stopped are different facts."""
        reply = self.cmd.format_reply(_occupancy(samples=0), [], BODY)
        assert 'Nothing heard' in reply
        assert '0%' not in reply

    def test_bandwidth_keeps_its_fraction(self):
        """62.5 kHz is not 62 kHz: the header states the modulation the estimate uses."""
        occupancy = _occupancy()
        occupancy.modulation = lambda: {'sf': 8, 'bw_khz': 62.5, 'cr_den': 8, 'preamble': 8}
        reply = self.cmd.format_reply(occupancy, [], BODY)
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
        reply = self.cmd.format_reply(occupancy, [], BODY)
        assert 'CR4/8' in reply
        assert 'CR8/5' not in reply

    def test_the_french_catalog_carries_the_reply_strings(self):
        """A bot whose language is fr answers in fr; a command whose strings ship only in en.json
        answers in English through the fallback, so the reader's setting is not what they get."""
        fr = json.loads(pathlib.Path('translations/fr.json').read_text(encoding='utf-8'))['commands']['airtime']
        assert set(CATALOG) <= set(fr)
        assert 'non mesuré' in fr['header']
        # The same notation as the English string and as the page: the coding rate is 4 over its
        # denominator, whatever language the reply is written in.
        assert 'CR4/{cr}' in fr['header']

    def test_an_integral_bandwidth_is_not_written_with_a_false_decimal(self):
        """125.0kHz claims a precision the reading does not have; 125kHz does not."""
        reply = self.cmd.format_reply(_occupancy(), [], BODY)
        assert '125kHz' in reply
        assert '125.0kHz' not in reply

    def test_senders_keep_the_airtime_order(self):
        """Ranking is by airtime: many short packets cost more than one long message."""
        senders = [_sender('B', 214, 12), _sender('A', 90, 1)]
        reply = self.cmd.format_reply(_occupancy(), senders, BODY)
        assert reply.index('B 214ms') < reply.index('A 90ms')

    def test_sender_list_is_dropped_not_cut(self):
        """A name cut mid-way reads as a different node; the reply says what is missing."""
        senders = [_sender('VeryLongMeshName1', 214, 12), _sender('AnotherLongName2', 90, 3)]
        # Long enough that the list itself does not fit, but the statement that it is
        # missing still does.
        reply = self.cmd.format_reply(_occupancy(), senders, 125)
        assert 'VeryLongMeshName1' not in reply
        assert 'dropped' in reply

    def test_reply_fits_one_body_when_the_list_fits(self):
        """The reply is one message; a second one spends airtime on the answer."""
        senders = [_sender('ALICE', 214, 12), _sender('BOB', 90, 3)]
        reply = self.cmd.format_reply(_occupancy(), senders, BODY)
        assert len(reply) <= BODY
        assert 'ALICE 214ms' in reply

    def test_names_are_capped_but_never_mid_name(self):
        """A capped name is still a whole name; the cap is a shape, not an ellipsis."""
        reply = self.cmd.format_reply(_occupancy(), [_sender('VeryLongMeshName1', 214, 12)], BODY)
        assert 'VeryLongMesh 214ms' in reply
        assert '...' not in reply


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
        ok, sent = self._run(_occupancy(senders=[_sender('ALICE', 214, 12)]), message)
        assert ok is True
        assert len(sent) == 1
        assert len(sent[0]) <= self.cmd.get_max_message_length(message)
        assert 'ALICE' in sent[0]

    def test_reply_that_does_not_fit_is_split_not_cut(self):
        """A long bot name leaves a body too small for the reply, so it travels in pieces.

        The pieces break at line boundaries, so a name arrives whole in one message.
        Cutting at the byte limit would deliver 'VeryLongMes' as if it were a different
        node, and the reader would have no way to tell.
        """
        self.bot.config.set('Bot', 'bot_name', 'TestBotWithAVeryLongMeshUserNameForBudget')
        self.cmd = AirtimeCommand(self.bot)
        senders = [_sender('VeryLongMeshName1', 214, 12), _sender('AnotherLongName2', 90, 3)]
        occupancy = _occupancy(senders=senders)
        message = mock_message(content='airtime')
        limit = self.cmd.get_max_message_length(message)
        full = self.cmd.format_reply(occupancy, senders, 400)
        assert limit < len(full), "the body has to be too small for the reply at all"

        ok, sent = self._run(occupancy, message)
        assert ok is True
        assert len(sent) > 1
        assert all(len(part) <= limit for part in sent)
        # Each capped name is whole inside one piece, not shared between two.
        assert any('VeryLongMesh 214ms' in part for part in sent)
