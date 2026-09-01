#!/usr/bin/env python3
"""
Lightweight language detection for the MeshCore Bot.

Mesh messages are short — usually a one-word greeting — so a keyword lookup
handles the common case reliably and cheaply.  For longer free-text messages
the optional ``langdetect`` package is used when installed (``pip install
meshcore-bot[lang]``); if it is missing, detection degrades gracefully to the
keyword map and the supplied fallback.

The set of languages a caller is willing to accept is passed in as
``supported`` (typically the base language codes for the translation files that
actually exist on disk).  A detection result is only returned when it is in
that set, so we never try to answer in a language we have no translations for.
"""

from typing import Iterable, Optional

# Common greetings mapped to their (base) language code.  These cover the
# single-word / short-phrase messages that ``langdetect`` handles poorly.
KEYWORD_LANG_MAP: dict[str, str] = {
    # Italian
    "ciao": "it", "salve": "it", "buongiorno": "it", "buonasera": "it",
    "buonanotte": "it", "grazie": "it", "prego": "it", "arrivederci": "it",
    "benvenuto": "it", "benvenuti": "it",
    # German
    "hallo": "de", "guten tag": "de", "guten morgen": "de", "guten abend": "de",
    "gute nacht": "de", "danke": "de", "bitte": "de", "tschüss": "de",
    "moin": "de", "servus": "de", "willkommen": "de",
    # French
    "bonjour": "fr", "bonsoir": "fr", "bonne nuit": "fr", "salut": "fr",
    "merci": "fr", "bienvenue": "fr", "au revoir": "fr",
    # Spanish
    "hola": "es", "buenos dias": "es", "buenas tardes": "es",
    "buenas noches": "es", "gracias": "es", "bienvenido": "es",
    # Dutch
    "hoi": "nl", "goedemorgen": "nl", "goedemiddag": "nl", "goedenavond": "nl",
    "welkom": "nl", "dankjewel": "nl", "dank je": "nl",
    # Portuguese
    "olá": "pt", "bom dia": "pt", "boa tarde": "pt", "boa noite": "pt",
    "obrigado": "pt", "obrigada": "pt", "bem-vindo": "pt",
    # Polish
    "cześć": "pl", "dzień dobry": "pl", "dobry wieczór": "pl",
    "dziękuję": "pl", "witaj": "pl",
    # English
    "hello": "en", "hi": "en", "hey": "en", "howdy": "en",
    "good morning": "en", "good afternoon": "en", "good evening": "en",
    "welcome": "en", "thanks": "en", "thank you": "en",
}

# Keywords ordered longest-first so multi-word phrases ("guten tag") win over
# a shorter prefix that might also appear.  Computed once at import time.
_KEYWORDS_BY_LENGTH = sorted(KEYWORD_LANG_MAP, key=len, reverse=True)


def _keyword_match(text_lower: str) -> Optional[str]:
    """Return the language for a leading greeting keyword, if any."""
    for keyword in _KEYWORDS_BY_LENGTH:
        if text_lower == keyword:
            return KEYWORD_LANG_MAP[keyword]
        # Match the keyword only when it stands alone at the start of the
        # message (followed by a space or punctuation), not as a substring of
        # a longer word.
        if text_lower.startswith(keyword) and text_lower[len(keyword)] in " !.,?;:":
            return KEYWORD_LANG_MAP[keyword]
    return None


def _langdetect_base(text: str) -> Optional[str]:
    """Detect a base language code via the optional ``langdetect`` package."""
    try:
        from langdetect import DetectorFactory, detect
        # Make results deterministic across runs for a given input.
        DetectorFactory.seed = 0
        detected = detect(text)
    except Exception:
        return None
    # langdetect returns codes like 'en', 'pt', or 'zh-cn'; keep the base.
    return detected.split("-")[0] if detected else None


def detect_language(
    text: str,
    supported: Optional[Iterable[str]] = None,
    fallback: str = "en",
) -> str:
    """Detect the language of ``text``.

    Args:
        text: The message text to classify.
        supported: Language codes we are willing to return (base codes such as
            ``{"en", "de", "fr"}``).  A detection outside this set is ignored
            and ``fallback`` is returned instead.  ``None`` accepts anything.
        fallback: Language code to return when detection is unavailable or the
            result is not supported.

    Returns:
        A base language code — a member of ``supported`` (when given) or
        ``fallback``.
    """
    if not text or not text.strip():
        return fallback

    supported_set = {s.split("-")[0] for s in supported} if supported else None

    def _accept(lang: Optional[str]) -> Optional[str]:
        if not lang:
            return None
        if supported_set is not None and lang not in supported_set:
            return None
        return lang

    text_lower = text.lower().strip()

    # 1. Fast, reliable path for short greetings.
    keyword_lang = _accept(_keyword_match(text_lower))
    if keyword_lang:
        return keyword_lang

    # 2. Statistical detection for longer free text, when available.
    if len(text_lower.split()) > 2:
        detected = _accept(_langdetect_base(text))
        if detected:
            return detected

    return fallback
