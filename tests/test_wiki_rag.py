#!/usr/bin/env python3
"""Tests for modules.wiki_rag."""

import json

from modules.wiki_rag import LocalWikiRag


def test_build_context_returns_empty_for_missing_index(tmp_path):
    rag = LocalWikiRag(str(tmp_path / "missing.jsonl"))
    assert rag.build_context("meshcore intro") == ""


def test_build_context_supports_json_array_index(tmp_path):
    index_file = tmp_path / "wiki.json"
    data = [
        {
            "path": "meshcore/Intro",
            "title": "Intro",
            "source_url": "https://wiki/s/meshcore/Intro",
            "chunks": ["Meshcore est un projet de réseau radio maillé."],
        }
    ]
    index_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    rag = LocalWikiRag(str(index_file))
    context = rag.build_context("projet meshcore")
    assert "Wiki.js context" in context
    assert "meshcore/Intro" in context


def test_build_context_returns_empty_on_empty_query(tmp_path):
    index_file = tmp_path / "wiki.jsonl"
    index_file.write_text("", encoding="utf-8")
    rag = LocalWikiRag(str(index_file))
    assert rag.build_context("") == ""


def test_build_context_prefers_higher_overlap(tmp_path):
    index_file = tmp_path / "wiki.jsonl"
    rows = [
        {
            "path": "meshcore/Intro",
            "title": "Intro",
            "source_url": "https://wiki/s/meshcore/Intro",
            "chunks": ["Meshcore routing radio configuration avancée."],
        },
        {
            "path": "meshcore/FAQ",
            "title": "FAQ",
            "source_url": "https://wiki/s/meshcore/FAQ",
            "chunks": ["Questions fréquentes générales."],
        },
    ]
    index_file.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
    rag = LocalWikiRag(str(index_file), max_chunks=1)
    context = rag.build_context("routing radio configuration")
    assert "meshcore/Intro" in context
    assert "meshcore/FAQ" not in context
