"""Tests for modules.lang_detector — keyword-first language detection."""

from modules import lang_detector
from modules.lang_detector import detect_language

SUPPORTED = {"en", "de", "fr", "es", "it", "nl", "pt", "pl"}


class TestKeywordDetection:
    """Short greetings resolve via the keyword map without langdetect."""

    def test_single_word_italian(self):
        assert detect_language("ciao", supported=SUPPORTED) == "it"

    def test_single_word_german(self):
        assert detect_language("hallo", supported=SUPPORTED) == "de"

    def test_case_insensitive(self):
        assert detect_language("HALLO", supported=SUPPORTED) == "de"

    def test_keyword_with_trailing_punctuation(self):
        assert detect_language("bonjour!", supported=SUPPORTED) == "fr"

    def test_keyword_followed_by_more_text(self):
        assert detect_language("hallo mijn vrienden", supported=SUPPORTED) == "de"

    def test_multiword_keyword_wins(self):
        assert detect_language("guten morgen", supported=SUPPORTED) == "de"

    def test_english_keyword(self):
        assert detect_language("hello there", supported=SUPPORTED) == "en"


class TestFallback:
    """Undetectable or unsupported input falls back safely."""

    def test_empty_string_returns_fallback(self):
        assert detect_language("", supported=SUPPORTED, fallback="en") == "en"

    def test_whitespace_returns_fallback(self):
        assert detect_language("   ", supported=SUPPORTED, fallback="de") == "de"

    def test_unknown_short_word_returns_fallback(self):
        # Not in the keyword map and too short for langdetect -> fallback.
        assert detect_language("xyz", supported=SUPPORTED, fallback="en") == "en"

    def test_keyword_not_in_supported_is_ignored(self):
        # 'ciao' -> it, but Italian is not offered here, so fall back.
        assert detect_language("ciao", supported={"en", "de"}, fallback="en") == "en"

    def test_substring_is_not_matched(self):
        # 'hint' starts with 'hi' but must not be detected as English greeting;
        # falls back rather than false-matching.
        assert detect_language("hinterland", supported=SUPPORTED, fallback="de") == "de"


class TestLangdetectPath:
    """Longer free text uses langdetect when available; degrades otherwise."""

    def test_long_text_detects_or_falls_back(self):
        # Whether langdetect is installed or not, the result must be a supported
        # code (detected) or the fallback — never an exception or unsupported code.
        result = detect_language(
            "das ist ein deutscher satz mit vielen woertern",
            supported=SUPPORTED,
            fallback="en",
        )
        assert result in SUPPORTED

    def test_supported_none_accepts_any(self):
        result = detect_language("ciao", supported=None, fallback="en")
        assert result == "it"

    def test_supported_statistical_result_is_used(self, monkeypatch):
        monkeypatch.setattr(lang_detector, "_langdetect_base", lambda _text: "de")
        assert detect_language(
            "dies ist ein ausreichend langer testsatz",
            supported=SUPPORTED,
            fallback="en",
        ) == "de"

    def test_unsupported_statistical_result_falls_back(self, monkeypatch):
        monkeypatch.setattr(lang_detector, "_langdetect_base", lambda _text: "sv")
        assert detect_language(
            "det har ar en tillrackligt lang testmening",
            supported=SUPPORTED,
            fallback="en",
        ) == "en"
