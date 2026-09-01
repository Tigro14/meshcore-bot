"""Command prefix parsing and message normalization."""

from __future__ import annotations

from configparser import ConfigParser

# Decorative single-character prefixes; used when parsing concatenated lists like "!~."
DECORATIVE_PREFIX_CHARS = frozenset('!.,/~')


def parse_command_prefixes(raw: str) -> list[str]:
    """Parse ``command_prefix`` config into an ordered prefix list.

    Rules:
    - Empty / unset → no prefixes (legacy bare + ``!`` mode).
    - Comma-separated → explicit list (e.g. ``!, ~, .`` or ``abc, xyz``).
    - All decorative chars and length > 1 → split per char (e.g. ``!~.``).
    - Otherwise → single prefix string (e.g. ``!``, ``abc``).
    """
    text = (raw or '').strip()
    if not text:
        return []
    if ',' in text:
        return [part.strip() for part in text.split(',') if part.strip()]
    if len(text) > 1 and all(char in DECORATIVE_PREFIX_CHARS for char in text):
        return list(text)
    return [text]


def load_command_prefix_settings(config: ConfigParser) -> tuple[list[str], bool]:
    """Load ``(prefixes, require_prefix)`` from ``[Bot]`` config."""
    raw = ''
    if config.has_section('Bot'):
        raw = (config.get('Bot', 'command_prefix', fallback='') or '').strip()
    prefixes = parse_command_prefixes(raw)
    if config.has_section('Bot') and config.has_option('Bot', 'require_command_prefix'):
        require_prefix = config.getboolean('Bot', 'require_command_prefix')
    else:
        require_prefix = True
    return prefixes, require_prefix


def find_matching_prefix(content: str, prefixes: list[str]) -> str | None:
    """Return the longest configured prefix that ``content`` starts with."""
    if not prefixes or not content:
        return None
    matched: str | None = None
    for prefix in sorted(prefixes, key=len, reverse=True):
        if content.startswith(prefix):
            if matched is None or len(prefix) > len(matched):
                matched = prefix
    return matched


def normalize_command_content(
    content: str,
    prefixes: list[str],
    *,
    require_prefix: bool,
) -> str | None:
    """Strip command prefix from content, or reject / pass through per ``require_prefix``.

    Returns:
        Stripped content, or ``None`` when the message should be ignored.
    """
    text = content.strip()
    if not prefixes:
        if text.startswith('!'):
            return text[1:].strip()
        return text

    matched = find_matching_prefix(text, prefixes)
    if matched is not None:
        return text[len(matched):].strip()

    if require_prefix:
        return None
    return text
