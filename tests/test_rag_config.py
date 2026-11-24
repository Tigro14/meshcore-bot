"""Migration and precedence of the shared RAG configuration."""
import configparser

from modules.assistant.rag_config import read_rag_config


def read(config, key, fallback=None, value_type='str'):
    def reader(section, option, fallback=None, value_type='str'):
        getter = {'bool': config.getboolean, 'int': config.getint,
                  'float': config.getfloat}.get(value_type, config.get)
        return getter(section, option, fallback=fallback)
    return read_rag_config(config, reader, key, fallback=fallback, value_type=value_type)


def test_legacy_profile_remains_readable():
    config = configparser.ConfigParser()
    config.read_dict({'Llm_Command': {'wiki_rag_enabled': 'true', 'wiki_rag_max_chunks': '3'}})
    assert read(config, 'wiki_rag_enabled', False, 'bool') is True
    assert read(config, 'wiki_rag_max_chunks', 2, 'int') == 3


def test_new_profile_and_explicit_values_override_legacy():
    config = configparser.ConfigParser()
    config.read_dict({'Llm_Command': {'wiki_rag_enabled': 'true', 'wiki_api_key': 'old'},
                      'Rag_Service': {'enabled': 'false', 'wiki_api_key': '',
                                      'wiki_rag_min_score': '7.5'}})
    assert read(config, 'wiki_rag_enabled', True, 'bool') is False
    assert read(config, 'wiki_api_key') == ''
    assert read(config, 'wiki_rag_min_score', 6, 'float') == 7.5


def test_new_section_can_enable_rag_without_legacy_options():
    config = configparser.ConfigParser()
    config.read_dict({'Rag_Service': {'enabled': 'true', 'wiki_site_url': 'https://wiki.example.org'}})
    assert read(config, 'wiki_rag_enabled', False, 'bool') is True
    assert read(config, 'wiki_site_url') == 'https://wiki.example.org'
    assert read(config, 'wiki_rag_max_chunks', 2, 'int') == 2


def test_missing_configuration_uses_defaults():
    config = configparser.ConfigParser()
    assert read(config, 'wiki_rag_enabled', False, 'bool') is False
