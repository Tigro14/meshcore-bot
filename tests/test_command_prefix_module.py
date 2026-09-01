#!/usr/bin/env python3
"""Unit tests for modules.command_prefix parsing and normalization."""

from configparser import ConfigParser

from modules.command_prefix import (
    find_matching_prefix,
    load_command_prefix_settings,
    normalize_command_content,
    parse_command_prefixes,
)


class TestParseCommandPrefixes:
    def test_empty(self):
        assert parse_command_prefixes('') == []
        assert parse_command_prefixes('   ') == []

    def test_single_char(self):
        assert parse_command_prefixes('!') == ['!']

    def test_single_multi_char(self):
        assert parse_command_prefixes('abc') == ['abc']

    def test_decorative_concatenated(self):
        assert parse_command_prefixes('!~.') == ['!', '~', '.']

    def test_comma_separated(self):
        assert parse_command_prefixes('!, ~, .') == ['!', '~', '.']

    def test_comma_separated_multi_char(self):
        assert parse_command_prefixes('abc, xyz') == ['abc', 'xyz']


class TestFindMatchingPrefix:
    def test_longest_match_wins(self):
        prefixes = ['ab', 'abc']
        assert find_matching_prefix('abctest', prefixes) == 'abc'

    def test_no_match(self):
        assert find_matching_prefix('ping', ['!']) is None


class TestNormalizeCommandContent:
    def test_legacy_bang_when_no_prefixes(self):
        assert normalize_command_content('!ping', [], require_prefix=False) == 'ping'
        assert normalize_command_content('ping', [], require_prefix=False) == 'ping'

    def test_strict_single_prefix(self):
        assert normalize_command_content('!ping', ['!'], require_prefix=True) == 'ping'
        assert normalize_command_content('ping', ['!'], require_prefix=True) is None

    def test_permissive_single_prefix(self):
        assert normalize_command_content('!ping', ['!'], require_prefix=False) == 'ping'
        assert normalize_command_content('ping', ['!'], require_prefix=False) == 'ping'

    def test_multi_prefix_strict(self):
        prefixes = ['!', '~', '.']
        assert normalize_command_content('~ping', prefixes, require_prefix=True) == 'ping'
        assert normalize_command_content('ping', prefixes, require_prefix=True) is None

    def test_multi_prefix_permissive(self):
        prefixes = ['!', '~', '.']
        assert normalize_command_content('ping', prefixes, require_prefix=False) == 'ping'
        assert normalize_command_content('.ping', prefixes, require_prefix=False) == 'ping'


class TestLoadCommandPrefixSettings:
    def test_defaults_require_true(self):
        config = ConfigParser()
        config.add_section('Bot')
        config.set('Bot', 'command_prefix', '!')
        prefixes, require = load_command_prefix_settings(config)
        assert prefixes == ['!']
        assert require is True

    def test_require_false_from_config(self):
        config = ConfigParser()
        config.add_section('Bot')
        config.set('Bot', 'command_prefix', '~')
        config.set('Bot', 'require_command_prefix', 'false')
        prefixes, require = load_command_prefix_settings(config)
        assert prefixes == ['~']
        assert require is False
