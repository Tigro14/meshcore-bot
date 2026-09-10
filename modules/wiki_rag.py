#!/usr/bin/env python3
"""
Local Wiki.js RAG retrieval utilities (keyword-based, no embeddings/vector DB).
"""

from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import Any


def _normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower()
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def _compact_whitespace(text: str) -> str:
    return " ".join((text or "").split()).strip()


class LocalWikiRag:
    """Simple lexical retriever over locally cached Wiki.js markdown."""

    _STOPWORDS = {
        "the",
        "and",
        "for",
        "with",
        "from",
        "that",
        "this",
        "dans",
        "avec",
        "pour",
        "vous",
        "nous",
        "les",
        "des",
        "une",
        "est",
        "sur",
        "par",
    }

    def __init__(
        self,
        index_path: str,
        *,
        max_chunks: int = 3,
        max_chars_per_chunk: int = 450,
        min_term_len: int = 3,
    ) -> None:
        self.index_path = Path(index_path)
        self.max_chunks = max(1, min(8, int(max_chunks)))
        self.max_chars_per_chunk = max(120, min(1200, int(max_chars_per_chunk)))
        self.min_term_len = max(2, min(8, int(min_term_len)))
        self._cached_mtime: float | None = None
        self._cached_chunks: list[dict[str, str]] = []

    def _tokenize(self, text: str) -> set[str]:
        tokens = _normalize_text(text).split()
        return {
            tok for tok in tokens if len(tok) >= self.min_term_len and tok not in self._STOPWORDS and not tok.isdigit()
        }

    def _load_chunks(self) -> list[dict[str, str]]:
        try:
            stat = self.index_path.stat()
        except OSError:
            self._cached_chunks = []
            self._cached_mtime = None
            return []

        if self._cached_mtime == stat.st_mtime:
            return self._cached_chunks

        raw_chunks: list[dict[str, str]] = []
        try:
            text = self.index_path.read_text(encoding="utf-8")
            if not text.strip():
                self._cached_chunks = []
                self._cached_mtime = stat.st_mtime
                return []
            if text.lstrip().startswith("["):
                entries = json.loads(text)
            else:
                entries = [json.loads(line) for line in text.splitlines() if line.strip()]
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                title = str(entry.get("title", "")).strip()
                path = str(entry.get("path", "")).strip()
                source = str(entry.get("source_url", "")).strip()
                chunks = entry.get("chunks")
                if isinstance(chunks, list) and chunks:
                    for chunk in chunks:
                        chunk_text = _compact_whitespace(str(chunk))
                        if not chunk_text:
                            continue
                        raw_chunks.append(
                            {
                                "title": title,
                                "path": path,
                                "source_url": source,
                                "content": chunk_text,
                            }
                        )
                    continue
                content = _compact_whitespace(str(entry.get("content", "")))
                if content:
                    raw_chunks.append(
                        {
                            "title": title,
                            "path": path,
                            "source_url": source,
                            "content": content,
                        }
                    )
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            raw_chunks = []

        self._cached_chunks = raw_chunks
        self._cached_mtime = stat.st_mtime
        return raw_chunks

    def build_context(self, query: str) -> str:
        query = (query or "").strip()
        if not query:
            return ""
        query_terms = self._tokenize(query)
        if not query_terms:
            return ""

        scored: list[tuple[int, dict[str, str]]] = []
        query_norm = _normalize_text(query)
        for chunk in self._load_chunks():
            content = chunk.get("content", "")
            if not content:
                continue
            chunk_terms = self._tokenize(content)
            overlap = query_terms.intersection(chunk_terms)
            if not overlap:
                continue
            title_path = f"{chunk.get('title', '')} {chunk.get('path', '')}".lower()
            score = len(overlap) * 4
            score += sum(2 for term in overlap if term in title_path)
            if query_norm and query_norm in _normalize_text(content):
                score += 5
            scored.append((score, chunk))

        if not scored:
            return ""

        scored.sort(key=lambda item: item[0], reverse=True)
        selected = [item[1] for item in scored[: self.max_chunks]]
        lines = ["Wiki.js context (local RAG, lexical, no external services):"]
        for i, item in enumerate(selected, start=1):
            text = item.get("content", "")
            if len(text) > self.max_chars_per_chunk:
                text = text[: self.max_chars_per_chunk].rstrip() + "..."
            label = item.get("path") or item.get("title") or f"source-{i}"
            source = item.get("source_url") or label
            lines.append(f"{i}. {label} | {source}\n{text}")
        return "\n".join(lines)
