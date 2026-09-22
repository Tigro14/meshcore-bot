#!/usr/bin/env python3
"""Universal Wiki.js collection and local lexical retrieval.

The collector is deliberately independent from any particular Wiki.js site.
It publishes an atomic JSONL index which the retriever reads locally.
"""

from __future__ import annotations

import difflib
import html
import json
import logging
import os
import re
import tempfile
import threading
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote, urljoin, urlparse

import requests

LIST_PAGES_WITH_LOCALE = """
query ListPages($locale: String!, $limit: Int!) {
  pages {
    list(locale: $locale, limit: $limit) {
      id
      path
      title
      locale
      contentType
      updatedAt
    }
  }
}
"""

LIST_PAGES = """
query ListPages($limit: Int!) {
  pages {
    list(limit: $limit) {
      id
      path
      title
      locale
      contentType
      updatedAt
    }
  }
}
"""

PAGE_SOURCE = """
query PageSource($id: Int!) {
  pages {
    single(id: $id) {
      id
      path
      title
      locale
      content
      contentType
      updatedAt
    }
  }
}
"""

DEFAULT_STOPWORDS = {
    "and",
    "avec",
    "dans",
    "des",
    "elle",
    "est",
    "for",
    "from",
    "les",
    "nous",
    "par",
    "pour",
    "que",
    "sur",
    "that",
    "the",
    "this",
    "une",
    "vous",
    "with",
}


class WikiRagError(RuntimeError):
    """Raised when a Wiki.js index cannot be refreshed safely."""


@dataclass(frozen=True)
class WikiRefreshResult:
    """Summary of one successfully published index."""

    listed: int
    selected: int
    fetched: int
    sections: int
    index_path: str


@dataclass(frozen=True)
class WikiRagSection:
    """One searchable section from the local index."""

    page_id: Any
    path: str
    locale: str
    page_title: str
    section_title: str
    section_index: int
    source_url: str
    updated_at: str
    content_type: str
    content: str


@dataclass(frozen=True)
class WikiRagMatch:
    """A scored section returned by the lexical retriever."""

    score: float
    section: WikiRagSection


@dataclass(frozen=True)
class WikiRagResult:
    """Structured result used to build the Wiki-specific LLM payload."""

    context: str
    matches: tuple[WikiRagMatch, ...]
    best_score: float


def _normalize_search(text: str) -> str:
    """Return an accent-insensitive search view while retaining technical punctuation."""
    value = unicodedata.normalize("NFKD", text or "")
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = value.casefold().replace("’", "'")
    value = re.sub(r"[^\w#./%:+-]+", " ", value, flags=re.UNICODE)
    return " ".join(value.split())


def _normalize_path(text: str) -> str:
    """Normalize a configured path for boundary-aware comparisons."""
    return _normalize_search(text).strip(" /")


def _encode_wiki_path(page_path: str) -> str:
    return "/".join(quote(part, safe="") for part in page_path.strip("/").split("/"))


def allowed_path(page_path: str, prefixes: Iterable[str]) -> bool:
    """Return whether a Wiki path is within an explicitly configured corpus."""
    normalized = _normalize_path(page_path)
    configured = tuple(prefix.strip() for prefix in prefixes if prefix.strip())
    if "*" in configured:
        return bool(normalized)
    for raw_prefix in configured:
        prefix = _normalize_path(raw_prefix)
        if prefix and (normalized == prefix or normalized.startswith(prefix + "/")):
            return True
    return False


class _ReadableHTMLParser(HTMLParser):
    """Convert API-provided HTML source to searchable text without scripts/styles."""

    _BLOCK_TAGS = {
        "address",
        "article",
        "aside",
        "blockquote",
        "br",
        "div",
        "footer",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "li",
        "main",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "tr",
        "ul",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag in {"script", "style"}:
            self._ignored_depth += 1
            return
        if not self._ignored_depth and tag in self._BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self._ignored_depth:
            self._ignored_depth -= 1
            return
        if not self._ignored_depth and tag in self._BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._ignored_depth:
            self._parts.append(data)

    def text(self) -> str:
        lines = [" ".join(line.split()) for line in "".join(self._parts).splitlines()]
        return "\n".join(line for line in lines if line).strip()


def _html_to_text(source: str) -> str:
    parser = _ReadableHTMLParser()
    parser.feed(source)
    parser.close()
    return parser.text()


def extract_guest_source(response_html: str) -> str:
    """Extract and decode the source shown by Wiki.js' public ``/s/`` view."""
    match = re.search(
        r"<code\b(?=[^>]*\bv-pre\b)[^>]*>(.*?)</code>",
        response_html or "",
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not match:
        raise WikiRagError("Wiki.js source view does not contain a <code v-pre> block; check read:source")
    source = html.unescape(match.group(1)).replace("\r\n", "\n").replace("\r", "\n").strip()
    if not source:
        raise WikiRagError("Wiki.js source view returned an empty <code v-pre> block")
    return source


def _split_plain_text(text: str, max_chars: int) -> list[str]:
    """Split oversized prose on line/word boundaries."""
    remaining = text.strip()
    parts: list[str] = []
    while len(remaining) > max_chars:
        candidate = remaining[:max_chars]
        boundary = max(candidate.rfind("\n"), candidate.rfind(". "), candidate.rfind(" "))
        if boundary < max_chars // 2:
            boundary = max_chars
        part = remaining[:boundary].strip()
        if part:
            parts.append(part)
        remaining = remaining[boundary:].strip()
    if remaining:
        parts.append(remaining)
    return parts


def _content_units(text: str) -> list[str]:
    """Split a section into paragraphs while keeping fenced code blocks intact."""
    units: list[str] = []
    current: list[str] = []
    fence: str | None = None
    for line in text.splitlines():
        stripped = line.lstrip()
        marker_match = re.match(r"(```+|~~~+)", stripped)
        if marker_match:
            marker = marker_match.group(1)
            if fence is None:
                if current:
                    units.append("\n".join(current).strip())
                    current = []
                fence = marker[0]
                current.append(line)
            else:
                current.append(line)
                if marker.startswith(fence):
                    units.append("\n".join(current).strip())
                    current = []
                    fence = None
            continue
        current.append(line)
        if fence is None and not stripped:
            unit = "\n".join(current).strip()
            if unit:
                units.append(unit)
            current = []
    if current:
        unit = "\n".join(current).strip()
        if unit:
            units.append(unit)
    return units


def _pack_section(text: str, max_chars: int) -> list[str]:
    """Pack prose units without cutting a fenced block."""
    chunks: list[str] = []
    current = ""
    for unit in _content_units(text):
        pieces = [unit]
        is_fenced = bool(re.match(r"^\s*(```|~~~)", unit))
        if len(unit) > max_chars and not is_fenced:
            pieces = _split_plain_text(unit, max_chars)
        for piece in pieces:
            candidate = f"{current}\n\n{piece}" if current else piece
            if len(candidate) <= max_chars:
                current = candidate
                continue
            if current:
                chunks.append(current)
            current = piece
    if current:
        chunks.append(current)
    return chunks


def split_source_sections(source: str, content_type: str, max_chars: int = 4000) -> list[tuple[str, str]]:
    """Return ``(heading, content)`` sections while preserving source structure."""
    source = (source or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not source:
        return []

    kind = (content_type or "markdown").casefold()
    if "html" in kind:
        source = _html_to_text(source)
        heading_pattern = None
    elif "asciidoc" in kind:
        heading_pattern = r"(?=^={1,4}\s+\S)"
    else:
        heading_pattern = r"(?=^#{1,3}\s+\S)"

    logical_sections = [source]
    if heading_pattern:
        logical_sections = [
            part.strip() for part in re.split(heading_pattern, source, flags=re.MULTILINE) if part.strip()
        ]

    result: list[tuple[str, str]] = []
    for logical in logical_sections:
        first_line = logical.splitlines()[0].strip() if logical.splitlines() else ""
        heading = re.sub(r"^(?:#{1,6}|={1,6})\s+", "", first_line).strip()
        if heading == first_line and len(logical_sections) == 1:
            heading = ""
        chunks = _pack_section(logical, max(500, max_chars))
        for chunk in chunks:
            result.append((heading, chunk))
    return result


class WikiJsSource:
    """Collect one configured Wiki.js instance into an atomic local index."""

    def __init__(
        self,
        *,
        site_url: str,
        index_path: str | Path,
        allowed_paths: Iterable[str],
        locale: str = "",
        api_key: str = "",
        refresh_interval_seconds: int = 86400,
        timeout: float = 20.0,
        verify_ssl: bool = True,
        max_section_chars: int = 4000,
        logger: Any | None = None,
        http_client: Any = requests,
    ) -> None:
        parsed = urlparse((site_url or "").strip())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("wiki_site_url must be an absolute HTTP(S) URL")
        configured_paths = tuple(path.strip() for path in allowed_paths if path.strip())
        if not configured_paths:
            raise ValueError("wiki_allowed_paths must contain prefixes or the explicit wildcard '*'")

        self.site_url = site_url.rstrip("/")
        self.index_path = Path(index_path)
        self.success_marker = self.index_path.with_name(self.index_path.name + ".last-success")
        self.allowed_paths = configured_paths
        self.locale = (locale or "").strip()
        self.api_key = (api_key or "").strip()
        self.refresh_interval_seconds = max(0, int(refresh_interval_seconds))
        self.timeout = max(1.0, min(300.0, float(timeout)))
        self.verify_ssl = bool(verify_ssl)
        self.max_section_chars = max(500, int(max_section_chars))
        self.logger = logger or logging.getLogger(__name__)
        self.http_client = http_client
        self._refresh_lock = threading.Lock()
        self._last_attempt_monotonic: float | None = None

    @property
    def api_mode(self) -> bool:
        return bool(self.api_key)

    def _graphql(self, query: str, variables: dict[str, object]) -> dict[str, Any]:
        headers = {"Content-Type": "application/json", "User-Agent": "meshcore-bot-wiki-rag/1"}
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        endpoint = urljoin(self.site_url + "/", "graphql")
        response = self.http_client.post(
            endpoint,
            json={"query": query, "variables": variables},
            headers=headers,
            timeout=self.timeout,
            verify=self.verify_ssl,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise WikiRagError("Wiki.js GraphQL response is not an object")
        errors = payload.get("errors")
        if errors:
            messages = [str(item.get("message", "GraphQL error")) for item in errors if isinstance(item, dict)]
            raise WikiRagError("Wiki.js GraphQL error: " + "; ".join(messages or [str(errors)]))
        data = payload.get("data")
        if not isinstance(data, dict):
            raise WikiRagError("Wiki.js GraphQL response has no data object")
        return data

    def list_pages(self) -> list[dict[str, Any]]:
        if self.locale:
            data = self._graphql(LIST_PAGES_WITH_LOCALE, {"locale": self.locale, "limit": 10000})
        else:
            data = self._graphql(LIST_PAGES, {"limit": 10000})
        pages = data.get("pages", {}).get("list", []) if isinstance(data.get("pages"), dict) else []
        if not isinstance(pages, list):
            raise WikiRagError("Wiki.js pages.list did not return a list")
        return [page for page in pages if isinstance(page, dict) and str(page.get("path", "")).strip()]

    def _guest_page_source(self, page: dict[str, Any]) -> tuple[str, str, str]:
        path = str(page.get("path", "")).strip("/")
        locale = str(page.get("locale") or self.locale).strip("/")
        source_path = "/".join(part for part in (quote(locale, safe=""), _encode_wiki_path(path)) if part)
        source_url = urljoin(self.site_url + "/", "s/" + source_path)
        response = self.http_client.get(
            source_url,
            headers={"User-Agent": "meshcore-bot-wiki-rag/1"},
            timeout=self.timeout,
            verify=self.verify_ssl,
        )
        response.raise_for_status()
        source = extract_guest_source(response.text)
        final_url = str(getattr(response, "url", "") or source_url)
        return source, str(page.get("contentType") or "markdown"), final_url

    def _api_page_source(self, page: dict[str, Any]) -> tuple[str, str, str]:
        page_id = page.get("id")
        if not isinstance(page_id, int):
            raise WikiRagError(f"Wiki.js page {page.get('path')!r} has no numeric id")
        data = self._graphql(PAGE_SOURCE, {"id": page_id})
        pages_data = data.get("pages")
        single = pages_data.get("single") if isinstance(pages_data, dict) else None
        if not isinstance(single, dict):
            raise WikiRagError(f"Wiki.js pages.single returned no page for id {page_id}")
        source = str(single.get("content") or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        if not source:
            raise WikiRagError(f"Wiki.js pages.single returned empty content for id {page_id}")
        content_type = str(single.get("contentType") or page.get("contentType") or "markdown")
        path = str(single.get("path") or page.get("path") or "").strip("/")
        locale = str(single.get("locale") or page.get("locale") or self.locale).strip("/")
        public_path = "/".join(part for part in (locale, _encode_wiki_path(path)) if part)
        return source, content_type, urljoin(self.site_url + "/", public_path)

    def fetch_page_source(self, page: dict[str, Any]) -> tuple[str, str, str]:
        if self.api_mode:
            return self._api_page_source(page)
        return self._guest_page_source(page)

    @staticmethod
    def _atomic_write(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_name = ""
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary_name = temporary.name
                temporary.write(text)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_name, path)
        finally:
            if temporary_name:
                Path(temporary_name).unlink(missing_ok=True)

    def refresh(self) -> WikiRefreshResult:
        """Build and atomically publish a complete index, or leave the old one untouched."""
        pages = self.list_pages()
        if not pages:
            raise WikiRagError("Wiki.js returned zero pages")
        selected = sorted(
            (page for page in pages if allowed_path(str(page.get("path", "")), self.allowed_paths)),
            key=lambda page: (
                str(page.get("locale") or self.locale).casefold(),
                str(page.get("path") or "").casefold(),
            ),
        )
        if not selected:
            raise WikiRagError("No Wiki.js pages match wiki_allowed_paths")

        records: list[dict[str, Any]] = []
        failures: list[str] = []
        fetched = 0
        for page in selected:
            path = str(page.get("path", "")).strip()
            try:
                source, content_type, source_url = self.fetch_page_source(page)
                sections = split_source_sections(source, content_type, self.max_section_chars)
                if not sections:
                    raise WikiRagError("source has no searchable content")
            except Exception as exc:
                failures.append(f"{path}: {exc}")
                continue
            fetched += 1
            for section_index, (section_title, content) in enumerate(sections):
                records.append(
                    {
                        "schema_version": 1,
                        "page_id": page.get("id"),
                        "path": path,
                        "locale": str(page.get("locale") or self.locale),
                        "page_title": str(page.get("title") or path),
                        "section_title": section_title,
                        "section_index": section_index,
                        "source_url": source_url,
                        "updated_at": str(page.get("updatedAt") or ""),
                        "content_type": content_type,
                        "content": content,
                    }
                )

        if failures:
            preview = "; ".join(failures[:3])
            extra = f"; and {len(failures) - 3} more" if len(failures) > 3 else ""
            raise WikiRagError(f"Wiki.js refresh incomplete ({len(failures)} page failures): {preview}{extra}")
        if fetched == 0 or not records:
            raise WikiRagError("Wiki.js refresh produced an empty index")

        serialized = "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records)
        self._atomic_write(self.index_path, serialized)
        marker = datetime.now(timezone.utc).isoformat() + "\n"
        self._atomic_write(self.success_marker, marker)
        result = WikiRefreshResult(
            listed=len(pages),
            selected=len(selected),
            fetched=fetched,
            sections=len(records),
            index_path=str(self.index_path),
        )
        self.logger.info(
            "Wiki.js RAG index published: mode=%s listed=%d selected=%d fetched=%d sections=%d path=%s",
            "api" if self.api_mode else "guest",
            result.listed,
            result.selected,
            result.fetched,
            result.sections,
            result.index_path,
        )
        return result

    def is_fresh(self) -> bool:
        if not self.index_path.is_file() or not self.success_marker.is_file():
            return False
        try:
            age = time.time() - self.success_marker.stat().st_mtime
        except OSError:
            return False
        return age < self.refresh_interval_seconds

    def ensure_fresh(self, *, force: bool = False) -> WikiRefreshResult | None:
        """Refresh a stale index; on failure log and retain the last good index."""
        if not force and self.refresh_interval_seconds <= 0:
            return None
        if not force and self.is_fresh():
            return None
        now = time.monotonic()
        if not force and self._last_attempt_monotonic is not None and now - self._last_attempt_monotonic < 60:
            return None
        with self._refresh_lock:
            if not force and self.is_fresh():
                return None
            now = time.monotonic()
            if not force and self._last_attempt_monotonic is not None and now - self._last_attempt_monotonic < 60:
                return None
            self._last_attempt_monotonic = now
            try:
                return self.refresh()
            except Exception as exc:
                self.logger.warning("Wiki.js RAG refresh failed; keeping existing index: %s", exc)
                return None


def _clip_context(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    budget = max(1, limit - 3)
    candidate = text[:budget]
    boundary = max(candidate.rfind("\n\n"), candidate.rfind("\n"), candidate.rfind(". "))
    if boundary >= budget // 2:
        candidate = candidate[:boundary]
    return candidate.rstrip() + "..."


class LocalWikiRag:
    """Deterministic lexical retriever over an atomic local Wiki.js index."""

    def __init__(
        self,
        index_path: str,
        *,
        max_chunks: int = 2,
        procedure_max_sections: int = 6,
        procedure_max_context_chars: int = 6000,
        max_chars_per_chunk: int = 1400,
        max_context_chars: int = 2400,
        min_term_len: int = 3,
        min_score: float = 6.0,
        relative_score: float = 0.55,
        fuzzy_ratio: float = 0.86,
        stopwords: Iterable[str] = (),
        aliases: dict[str, str] | None = None,
        source: WikiJsSource | None = None,
        logger: Any | None = None,
    ) -> None:
        self.index_path = Path(index_path)
        self.max_chunks = max(1, min(8, int(max_chunks)))
        self.procedure_max_sections = max(1, min(12, int(procedure_max_sections)))
        self.procedure_max_context_chars = max(300, min(12000, int(procedure_max_context_chars)))
        self.max_chars_per_chunk = max(120, min(4000, int(max_chars_per_chunk)))
        self.max_context_chars = max(300, min(12000, int(max_context_chars)))
        self.min_term_len = max(2, min(12, int(min_term_len)))
        self.min_score = max(0.0, float(min_score))
        self.relative_score = max(0.0, min(1.0, float(relative_score)))
        self.fuzzy_ratio = max(0.5, min(1.0, float(fuzzy_ratio)))
        self.stopwords = DEFAULT_STOPWORDS | {_normalize_search(word) for word in stopwords if _normalize_search(word)}
        self.aliases = {
            _normalize_search(key): _normalize_search(value)
            for key, value in (aliases or {}).items()
            if _normalize_search(key) and _normalize_search(value)
        }
        self.source = source
        self.logger = logger or logging.getLogger(__name__)
        self._cached_mtime_ns: int | None = None
        self._cached_sections: list[WikiRagSection] = []

    def ensure_fresh(self, *, force: bool = False) -> WikiRefreshResult | None:
        if self.source is None:
            return None
        return self.source.ensure_fresh(force=force)

    def _tokenize(self, text: str) -> list[str]:
        raw_tokens = re.findall(r"#?[\w][\w./%:+-]*", _normalize_search(text), flags=re.UNICODE)
        tokens: list[str] = []
        for token in raw_tokens:
            token = self.aliases.get(token, token)
            if len(token) >= self.min_term_len and token not in self.stopwords and not token.isdigit():
                tokens.append(token)
        return tokens

    @staticmethod
    def _section_from_entry(entry: dict[str, Any], content: str, section_index: int) -> WikiRagSection:
        return WikiRagSection(
            page_id=entry.get("page_id", entry.get("id")),
            path=str(entry.get("path", "")).strip(),
            locale=str(entry.get("locale", "")).strip(),
            page_title=str(entry.get("page_title", entry.get("title", ""))).strip(),
            section_title=str(entry.get("section_title", "")).strip(),
            section_index=int(entry.get("section_index", section_index) or 0),
            source_url=str(entry.get("source_url", "")).strip(),
            updated_at=str(entry.get("updated_at", entry.get("updatedAt", ""))).strip(),
            content_type=str(entry.get("content_type", entry.get("contentType", "markdown"))).strip(),
            content=content.strip(),
        )

    def _load_sections(self) -> list[WikiRagSection]:
        try:
            stat = self.index_path.stat()
        except OSError:
            return self._cached_sections
        if self._cached_mtime_ns == stat.st_mtime_ns:
            return self._cached_sections

        try:
            text = self.index_path.read_text(encoding="utf-8")
            if not text.strip():
                raise ValueError("index is empty")
            entries = (
                json.loads(text)
                if text.lstrip().startswith("[")
                else [json.loads(line) for line in text.splitlines() if line.strip()]
            )
            if not isinstance(entries, list):
                raise ValueError("index root is not a list or JSONL sequence")
            sections: list[WikiRagSection] = []
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                chunks = entry.get("chunks")
                if isinstance(chunks, list) and chunks:
                    for index, chunk in enumerate(chunks):
                        content = str(chunk).strip()
                        if content:
                            sections.append(self._section_from_entry(entry, content, index))
                    continue
                content = str(entry.get("content", "")).strip()
                if content:
                    sections.append(self._section_from_entry(entry, content, len(sections)))
            if not sections:
                raise ValueError("index has no searchable sections")
        except (OSError, TypeError, ValueError) as exc:
            self.logger.warning("Unable to load Wiki.js RAG index %s; keeping previous index: %s", self.index_path, exc)
            self._cached_mtime_ns = stat.st_mtime_ns
            return self._cached_sections

        self._cached_sections = sections
        self._cached_mtime_ns = stat.st_mtime_ns
        self.logger.info("Loaded Wiki.js RAG index: sections=%d path=%s", len(sections), self.index_path)
        return sections

    def _score(
        self,
        query_terms: list[str],
        query_phrases: list[str],
        section: WikiRagSection,
        *,
        requests_values: bool = False,
    ) -> float:
        content_norm = _normalize_search(section.content)
        content_tokens = self._tokenize(section.content)
        content_counts = Counter(content_tokens)
        page_title_norm = _normalize_search(section.page_title)
        section_title_norm = _normalize_search(section.section_title)
        path_norm = _normalize_search(section.path.replace("/", " "))
        page_title_terms = set(self._tokenize(section.page_title))
        section_title_terms = set(self._tokenize(section.section_title))
        path_terms = set(self._tokenize(section.path.replace("/", " ")))

        score = 0.0
        for term in set(query_terms):
            if term in section_title_terms:
                score += 12
            if term in page_title_terms:
                score += 8
            if term in path_terms:
                score += 4
            count = content_counts.get(term, 0)
            if count:
                score += min(count, 4) * 3
            elif len(term) >= 5 and any(
                difflib.SequenceMatcher(None, term, candidate).ratio() >= self.fuzzy_ratio
                for candidate in content_counts
                if len(candidate) >= 5
            ):
                score += 2

        for phrase in query_phrases:
            if phrase in section_title_norm:
                score += 10
            elif phrase in page_title_norm:
                score += 8
            elif phrase in path_norm:
                score += 6
            elif phrase in content_norm:
                score += 5
        # Action words carry more intent than the device noun. Without this,
        # a conceptual diagram headed "Regions" can outrank the section that
        # actually explains how to add one.
        action_families = (
            # A manual may call the same operation "add", "configure" or
            # "set". They express one setup/mutation intent and must compete
            # on the device and the usable evidence, not the exact verb.
            (r"\b(?:ajout\w*|add\w*|associ\w*|assign\w*|configur\w*|parametr\w*|setup|set)\b",),
            (r"\b(?:retir\w*|supprim\w*|remove\w*|delet\w*)\b",),
            (r"\b(?:activ\w*|enable\w*)\b",),
            (r"\b(?:desactiv\w*|disable\w*)\b",),
            (r"\b(?:install\w*)\b",),
        )
        query_norm = " ".join(query_terms)
        section_norm = " ".join((section_title_norm, page_title_norm, content_norm))
        if self._markdown_table_section(section):
            table_subjects = self._table_subject_terms(section)
            query_subjects = set(query_terms) | {term.rstrip("s") for term in query_terms}
            if table_subjects & query_subjects:
                score += 16
        if requests_values and self._structured_values_section(section):
            # Lists of allowed values are often stored in fenced ``text``
            # blocks rather than Markdown tables. Treat them as evidence, not
            # as diagrams, and prefer the section whose heading names the
            # requested subject.
            score += 14
            subjects = {
                term.rstrip("s")
                for term in page_title_terms | section_title_terms | path_terms
            }
            query_subjects = {term.rstrip("s") for term in query_terms}
            # Reward each metadata subject. A device-qualified request such as
            # "regions for a Companion" must outrank a generic Regions page.
            score += min(20, 10 * len(subjects & query_subjects))
        for (pattern,) in action_families:
            if re.search(pattern, query_norm):
                score += 12 if re.search(pattern, section_title_norm) else 0
                score += 7 if re.search(pattern, section_norm) else -6
                if self._fenced_command_blocks(section):
                    # Operational questions benefit more from executable
                    # commands than conceptual prose or UI navigation.
                    score += 16
        return score

    @staticmethod
    def _navigation_section(section: WikiRagSection) -> bool:
        """Recognize link directories, not prose that merely cites a source."""
        if re.search(r"^\s*(?:```|~~~)", section.content, re.MULTILINE):
            return False
        lines = [line.strip() for line in section.content.splitlines()
                 if line.strip() and not line.lstrip().startswith(("#", "{."))
                 and line.strip() not in {"---", "***"}]
        links = sum(bool(re.match(r"^(?:[-*+]\s+)?\[[^\]]+\]\([^)]+\)\s*$", line))
                    for line in lines)
        return links >= 2 and links / max(1, len(lines)) >= 0.6

    @staticmethod
    def _table_subject_terms(section: WikiRagSection) -> set[str]:
        content = section.content.replace("\\n", "\n")
        for raw_line in content.splitlines():
            line = raw_line.strip().lstrip(">").strip().strip("|")
            cells = [cell.strip() for cell in line.split("|")]
            if len(cells) < 2 or re.fullmatch(r"[-:\s]+", cells[0]):
                continue
            terms = set(re.findall(r"[\w-]+", _normalize_search(cells[0])))
            return terms | {term.rstrip("s") for term in terms}
        return set()

    @staticmethod
    def _markdown_table_section(section: WikiRagSection) -> bool:
        content = section.content.replace("\\n", "\n")
        compact_rows = re.findall(
            r"(?m)^\s*>?\s*\|?\s*[^|\n]{1,40}\s*\|\s*[^|\n]{1,80}\s*\|?\s*$",
            content,
        )
        if len(compact_rows) >= 3:
            return True
        lines = [line.strip() for line in content.splitlines() if line.strip()]
        # Accept both Markdown tables and the compact tables commonly embedded
        # in fenced ``text`` blocks by Wiki.js.
        pipe_rows = sum("|" in line for line in lines)
        separator = any(
            "|" in line and re.fullmatch(r"[-:|\s]{5,}", line)
            for line in lines
        )
        tabular_rows = sum(line.count("|") >= 2 for line in lines)
        return separator or tabular_rows >= 3 or pipe_rows >= 4

    @staticmethod
    def _fenced_value_lists(section: WikiRagSection) -> list[list[str]]:
        """Return compact value lists without confusing UI paths for values."""
        lists: list[list[str]] = []
        content = section.content.replace("\\n", "\n")
        for block in re.findall(
            r"(?ms)^\s*(?:```|~~~)[^\n]*\n(.*?)^\s*(?:```|~~~)\s*$",
            content,
        ):
            values: list[str] = []
            lines = [line.strip() for line in block.splitlines() if line.strip()]
            for line in lines:
                # Configuration values and identifiers are compact. Arrows,
                # prose, commands and tree branches are deliberately rejected.
                if re.fullmatch(r"[#@]?[\w][\w./%:+-]{0,31}", line, flags=re.UNICODE):
                    if line not in values:
                        values.append(line)
            if 2 <= len(values) <= 20 and len(values) / max(1, len(lines)) >= 0.6:
                lists.append(values)
        return lists

    @staticmethod
    def _fenced_command_blocks(section: WikiRagSection) -> list[list[str]]:
        """Return compact, coherent command blocks from fenced Wiki content."""
        commands: list[list[str]] = []
        content = section.content.replace("\\n", "\n")
        for block in re.findall(
            r"(?ms)^\s*(?:```|~~~)[^\n]*\n(.*?)^\s*(?:```|~~~)\s*$",
            content,
        ):
            lines = [line.strip() for line in block.splitlines() if line.strip()]
            if not 2 <= len(lines) <= 12:
                continue
            if any(len(line) > 160 or re.search(r"(?:-->|<--|\|\||[▼▲▶◀])", line) for line in lines):
                continue
            tokens = [re.findall(r"\S+", line) for line in lines]
            if any(len(parts) < 2 for parts in tokens):
                continue
            # A command sequence normally shares the executable or namespace
            # (for example: region def/default/save). Requiring that common
            # prefix rejects menus, prose, value lists and ASCII diagrams.
            prefixes = [parts[0].casefold() for parts in tokens]
            if len(set(prefixes)) != 1 or not re.fullmatch(r"[\w./:+-]+", prefixes[0]):
                continue
            commands.append(lines)
        return commands

    @classmethod
    def _structured_values_section(cls, section: WikiRagSection) -> bool:
        return cls._markdown_table_section(section) or bool(cls._fenced_value_lists(section))

    @staticmethod
    def _diagram_section(section: WikiRagSection) -> bool:
        """Recognize diagrams and table-only schemas that are poor answer text."""
        content = section.content
        if re.search(r"(?im)^\s*```\s*(?:mermaid|graph|flowchart|sequenceDiagram)\b", content):
            return True
        # Wiki pages also use fenced ``text`` blocks for ASCII flow charts.
        # Judge each fenced block independently so surrounding prose cannot
        # dilute the structural-line ratio.
        for block in re.findall(r"(?ms)^\s*(?:```|~~~)[^\n]*\n(.*?)^\s*(?:```|~~~)\s*$", content):
            block_lines = [line.strip() for line in block.splitlines() if line.strip()]
            if not block_lines:
                continue
            structural = sum(bool(re.search(
                r"(?:-->|<--|\|\||[▼▲▶◀]|^[|+\\/_ -]{3,}$|^\|.*\|$)", line
            )) for line in block_lines)
            if structural >= 2 and structural / len(block_lines) >= 0.35:
                return True
        lines = [line.strip() for line in content.splitlines() if line.strip()]
        if not lines:
            return False
        structural = sum(bool(re.search(
            r"(?:-->|<--|\|\||\{\{|\}\}|^\|.*\|$|^\s*[-:| ]{5,}\s*$)", line
        )) for line in lines)
        return structural >= 2 and structural / len(lines) >= 0.45

    def _page_evidence(self, terms: list[str], section: WikiRagSection) -> float:
        """Use metadata rather than page length or repeated body keywords."""
        title = set(self._tokenize(section.page_title))
        heading = set(self._tokenize(section.section_title))
        path = set(self._tokenize(section.path.replace("/", " ")))
        title |= {term.rstrip("s") for term in title}
        heading |= {term.rstrip("s") for term in heading}
        path |= {term.rstrip("s") for term in path}
        # Action verbs describe what to do, not which device or subject page
        # should supply the answer. Ignoring them for page focus prevents an
        # exact "Add ..." heading on the wrong device from tying the page whose
        # title/path actually names the requested device.
        subject_terms = {
            term
            for term in terms
            if not re.fullmatch(
                r"configur\w*|install\w*|parametr\w*|setup|set|ajout\w*|add\w*|"
                r"associ\w*|assign\w*|retir\w*|supprim\w*|remove\w*|activ\w*|"
                r"enable\w*|disable\w*",
                term,
            )
        }
        evidence = sum(
            8 * (term in title) + 12 * (term in heading) + 4 * (term in path)
            for term in subject_terms
        )
        # In natural-language relation questions the final subject commonly
        # qualifies the requested device or target ("regions for a repeater",
        # "policy on a gateway"). Prefer pages that name that qualifier in
        # their title/path over concept pages that only mention it in a heading.
        qualifier = next((term for term in reversed(terms) if term in subject_terms), "")
        if qualifier and (qualifier in title or qualifier in path):
            evidence += 20
        return evidence

    @staticmethod
    def _page_key(section: WikiRagSection) -> tuple[str, str, str]:
        # Keep locales/sites separate, including externally managed corpora.
        return section.source_url, section.locale, section.path

    def _procedure_page(self, query: str, terms: list[str], sections: list[WikiRagMatch], primary):
        """Expand only a broad setup question about an unambiguous page topic.

        Specific parameter questions and definitions keep lexical retrieval.
        This vocabulary describes intents, never devices or regional settings.
        """
        normalized = _normalize_search(query)
        if not re.search(
            r"\b(configur\w*|install\w*|parametr\w*|setup|set up|ajout\w*|add\w*|"
            r"associ\w*|assign\w*|retir\w*|supprim\w*|remove\w*|activ\w*|enable\w*|disable\w*)\b",
            normalized,
        ):
            return False
        if re.search(r"\b(pourquoi|why|definition|what is|qu.est.ce)\b", normalized):
            return False
        if primary is None:
            return False
        page = next(m.section for m in sections if self._page_key(m.section) == primary)
        topic = set(self._tokenize(page.page_title + ' ' + page.path.replace('/', ' ')))
        topic |= {t[:-1] for t in topic if t.endswith('s')}
        intent = {'bonjour', 'salut', 'hello', 'please', 'svp', 'comment', 'how', 'faire', 'do'}
        subject = {t for t in terms if t not in intent and not re.fullmatch(
            r'configur\w*|install\w*|parametr\w*|setup|ajout\w*|add\w*|associ\w*|assign\w*|'
            r'retir\w*|supprim\w*|remove\w*|activ\w*|enable\w*|disable\w*', t)}
        return bool(subject) and subject <= topic

    @staticmethod
    def _procedure_content(content: str) -> str:
        """Omit optional collapsed explanations, but retain cautions/conditions."""
        def compact(match):
            text = match.group(0)
            if re.search(r'(?i)warning|important|attention|prerequis|prerequisite|'
                         r'\bif\b|\bsi\b|must|require|adapt|avant|before', text):
                return text
            return ''
        return re.sub(r'<details\b[^>]*>.*?</details>', compact, content,
                      flags=re.IGNORECASE | re.DOTALL).strip()

    @staticmethod
    def _procedural_section(section: WikiRagSection) -> bool:
        heading = _normalize_search(section.section_title)
        return bool(re.search(r'^\s*\d+[.)]', heading) or re.search(
            r'(?im)^\s*>.*(?:important|warning|attention|prerequis)', section.content) or re.search(
            r'\b(prerequis|prerequisites?|requirements?|parametres?|settings?|verification|'
            r'verify|checks?|avertissement|warnings?|sauvegard\w*|save|ajout\w*|add\w*|'
            r'associ\w*|assign\w*|retir\w*|supprim\w*|remove\w*|activ\w*|enable\w*)\b', heading)
            or (bool(re.search(r'(?m)^\s*```', section.content))
                and not LocalWikiRag._diagram_section(section)))

    def retrieve(self, query: str) -> WikiRagResult | None:
        query_terms = self._tokenize(query)
        if not query_terms:
            return None
        query_phrases = [f"{query_terms[index]} {query_terms[index + 1]}" for index in range(len(query_terms) - 1)]

        query_norm = _normalize_search(query)
        requests_values = bool(re.search(
            r"\b(?:donne|liste|quels?|quelles?|list|show|values?|options?|supported)\b",
            query_norm,
        ))
        requests_diagram = bool(re.search(r"\b(?:diagramme|schema|tableau|diagram|schema|table)\b", query_norm))
        scored: list[WikiRagMatch] = []
        pages: dict[tuple[str, str, str], float] = {}
        for section in self._load_sections():
            score = self._score(
                query_terms,
                query_phrases,
                section,
                requests_values=requests_values,
            )
            navigation = self._navigation_section(section)
            diagram = self._diagram_section(section)
            useful_table = requests_values and self._structured_values_section(section)
            if navigation:
                score *= 0.25
            if diagram and not requests_diagram and not useful_table:
                score *= 0.15
            if score > 0:
                scored.append(WikiRagMatch(score=score, section=section))
                key = self._page_key(section)
                evidence = self._page_evidence(query_terms, section)
                penalty = 0.25 if navigation else (0.15 if diagram and not useful_table else 1.0)
                pages[key] = max(pages.get(key, 0.0), evidence * penalty)
        if not scored:
            return None
        scored.sort(key=lambda match: (-match.score, match.section.section_index, match.section.path))
        best_score = scored[0].score
        if best_score < self.min_score:
            return None

        threshold = max(self.min_score, best_score * self.relative_score)
        # Focus only when metadata clearly identifies a page. Ambiguous or
        # multi-page questions retain the global ranking. Never promote a
        # section that fails the existing relevance thresholds.
        page_ranking = sorted(pages.items(), key=lambda item: -item[1])
        primary = None
        if not requests_values and len(set(query_terms)) >= 2 and page_ranking[0][1] >= 12:
            runner_up = page_ranking[1][1] if len(page_ranking) > 1 else 0
            if page_ranking[0][1] > runner_up * 1.25:
                primary = page_ranking[0][0]
        if primary is not None:
            scored.sort(key=lambda match: (
                self._page_key(match.section) != primary,
                -match.score, match.section.section_index, match.section.path,
            ))
        procedure = self._procedure_page(query, query_terms, scored, primary)
        if procedure:
            # Page relevance was established above. Numbered steps and their
            # prerequisites may legitimately share none of the query's words.
            procedural = [m for m in scored if self._page_key(m.section) == primary
                          and self._procedural_section(m.section)]
            if procedural:
                scored = sorted(procedural, key=lambda m: m.section.section_index)
            else:
                procedure = False
        if not procedure and primary is not None:
            page = next(m.section for m in scored if self._page_key(m.section) == primary)
            topic = set(self._tokenize(page.page_title + ' ' + page.path.replace('/', ' ')))
            topic |= {t[:-1] for t in topic if t.endswith('s')}
            specific = {t for t in query_terms if t not in topic
                        and t not in {'bonjour', 'salut', 'hello', 'comment', 'how', 'please', 'svp'}
                        and not re.fullmatch(r'configur\w*|install\w*|parametr\w*|setup', t)}
            focused = [m for m in scored if self._page_key(m.section) == primary
                       and specific & set(self._tokenize(m.section.section_title + ' ' + m.section.content))
                       and m.score >= self.min_score]
            if specific and focused:
                scored = focused
                threshold = max(self.min_score, max(m.score for m in focused) * self.relative_score)
        selected: list[WikiRagMatch] = []
        seen: set[str] = set()
        for match in scored:
            if not procedure and match.score < threshold:
                continue
            signature = _normalize_search(match.section.content)
            if signature in seen:
                continue
            seen.add(signature)
            selected.append(match)
            if len(selected) >= (self.procedure_max_sections if procedure else self.max_chunks):
                break
        if not selected:
            return None

        begin_marker = "WIKI_REFERENCE_DATA_BEGIN"
        end_marker = "WIKI_REFERENCE_DATA_END"
        parts = [begin_marker]
        # Reserve enough room for the closing marker. Build the context to the
        # configured budget instead of truncating the final string mid-source.
        context_limit = self.procedure_max_context_chars if procedure else self.max_context_chars
        remaining = context_limit - len(begin_marker) - len(end_marker) - 104
        included: list[WikiRagMatch] = []
        for number, match in enumerate(selected, start=1):
            section = match.section
            label = section.page_title or section.path or f"source-{number}"
            metadata = [f"source {number}", f"page: {label}"]
            if section.section_title:
                metadata.append(f"section: {section.section_title}")
            if section.path:
                metadata.append(f"path: {section.path}")
            if section.locale:
                metadata.append(f"locale: {section.locale}")
            if section.source_url:
                metadata.append(f"url: {section.source_url}")
            prefix = "\n".join(metadata) + "\ncontent:\n"
            content_budget = remaining - len(prefix) - 2
            if not procedure:
                content_budget = min(self.max_chars_per_chunk, content_budget)
            if content_budget < 80:
                break
            content = self._procedure_content(section.content) if procedure else section.content
            if procedure and len(content) > content_budget:
                # Never provide half a step with its save command or warning missing.
                break
            block = prefix + _clip_context(content, content_budget)
            parts.append(block)
            included.append(match)
            remaining -= len(block) + 2
        if not included:
            return None
        if procedure and len(included) < len(scored):
            parts.append("INCOMPLETE PROCEDURE: context budget omitted steps; do not claim completeness.")
        parts.append(end_marker)
        context = "\n\n".join(parts)
        return WikiRagResult(context=context, matches=tuple(included), best_score=best_score)

    def build_context(self, query: str) -> str:
        result = self.retrieve(query)
        return result.context if result else ""
