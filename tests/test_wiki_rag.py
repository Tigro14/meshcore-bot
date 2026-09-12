#!/usr/bin/env python3
"""Tests for the universal Wiki.js collector and lexical retriever."""

import json
import os
from unittest.mock import Mock

import pytest
import requests

from modules.wiki_rag import (
    LocalWikiRag,
    WikiJsSource,
    WikiRagError,
    allowed_path,
    extract_guest_source,
    split_source_sections,
)


class FakeResponse:
    def __init__(self, *, payload=None, text="", status_code=200):
        self._payload = payload
        self.text = text
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


def page_list(*pages):
    return FakeResponse(payload={"data": {"pages": {"list": list(pages)}}})


def test_allowed_paths_are_boundary_aware_and_accent_insensitive():
    assert allowed_path("Guides/Réseau/install", ["guides/reseau"])
    assert allowed_path("guides/reseau", ["guides/reseau"])
    assert not allowed_path("guides/reseau-old", ["guides/reseau"])
    assert allowed_path("anything/visible", ["*"])
    assert not allowed_path("", ["*"])


def test_extract_guest_source_preserves_markup_newlines_and_entities():
    source = extract_guest_source("<html><code class='source' v-pre># Titre\n\n`#canal` &amp; /commande</code></html>")
    assert source == "# Titre\n\n`#canal` & /commande"


def test_extract_guest_source_requires_read_source_block():
    with pytest.raises(WikiRagError, match="read:source"):
        extract_guest_source("<html><p>Rendered page only</p></html>")


def test_split_source_sections_uses_headings_and_keeps_fenced_blocks():
    source = """# Install

Intro.

## Command

```ini
name = #alpha
value = 123
```

End.
"""
    sections = split_source_sections(source, "markdown", max_chars=500)
    assert [heading for heading, _content in sections] == ["Install", "Command"]
    command_content = sections[1][1]
    assert "```ini\nname = #alpha\nvalue = 123\n```" in command_content


def test_guest_collector_is_universal_and_sends_no_authorization(tmp_path):
    client = Mock()
    client.post.return_value = page_list(
        {
            "id": 1,
            "path": "handbook/install",
            "title": "Install",
            "locale": "fr",
            "contentType": "markdown",
            "updatedAt": "2026-01-01T00:00:00Z",
        },
        {"id": 2, "path": "private/admin", "title": "Admin", "contentType": "markdown"},
    )
    client.get.return_value = FakeResponse(text="<code v-pre># Install\n\nRun `bot --start` &amp; wait.</code>")
    index = tmp_path / "corpus.jsonl"
    source = WikiJsSource(
        site_url="https://docs.example.net/wiki",
        index_path=index,
        allowed_paths=["handbook"],
        locale="fr",
        api_key="",
        http_client=client,
    )

    result = source.refresh()

    assert result.listed == 2
    assert result.selected == result.fetched == 1
    assert result.sections == 1
    record = json.loads(index.read_text(encoding="utf-8"))
    assert record["path"] == "handbook/install"
    assert record["content"] == "# Install\n\nRun `bot --start` & wait."
    assert record["locale"] == "fr"
    assert record["source_url"] == "https://docs.example.net/wiki/s/fr/handbook/install"
    assert index.with_name("corpus.jsonl.last-success").is_file()
    assert "Authorization" not in client.post.call_args.kwargs["headers"]
    assert "Authorization" not in client.get.call_args.kwargs["headers"]


def test_api_collector_uses_pages_single_and_bearer_key(tmp_path):
    client = Mock()
    client.post.side_effect = [
        page_list({"id": 7, "path": "manual/start", "title": "Start", "contentType": "markdown"}),
        FakeResponse(
            payload={
                "data": {
                    "pages": {
                        "single": {
                            "id": 7,
                            "path": "manual/start",
                            "title": "Start",
                            "locale": "en",
                            "content": "# Start\n\nUse device_id ABC-123.",
                            "contentType": "markdown",
                            "updatedAt": "2026-01-01T00:00:00Z",
                        }
                    }
                }
            }
        ),
    ]
    index = tmp_path / "api.jsonl"
    source = WikiJsSource(
        site_url="https://kb.other.example",
        index_path=index,
        allowed_paths=["manual"],
        api_key="secret",
        http_client=client,
    )

    result = source.refresh()

    assert result.fetched == 1
    assert client.get.call_count == 0
    assert client.post.call_count == 2
    for call in client.post.call_args_list:
        assert call.kwargs["headers"]["Authorization"] == "Bearer secret"
    assert client.post.call_args_list[1].kwargs["json"]["variables"] == {"id": 7}
    assert "ABC-123" in index.read_text(encoding="utf-8")


def test_graphql_errors_are_not_accepted(tmp_path):
    client = Mock()
    client.post.return_value = FakeResponse(payload={"errors": [{"message": "forbidden"}]})
    source = WikiJsSource(
        site_url="https://wiki.example",
        index_path=tmp_path / "wiki.jsonl",
        allowed_paths=["*"],
        http_client=client,
    )
    with pytest.raises(WikiRagError, match="forbidden"):
        source.refresh()


def test_partial_refresh_keeps_the_previous_index(tmp_path):
    index = tmp_path / "wiki.jsonl"
    index.write_text("previous-good-index\n", encoding="utf-8")
    client = Mock()
    client.post.return_value = page_list(
        {"id": 1, "path": "manual/one", "title": "One", "contentType": "markdown"},
        {"id": 2, "path": "manual/two", "title": "Two", "contentType": "markdown"},
    )
    client.get.side_effect = [
        FakeResponse(text="<code v-pre># One\n\nComplete source.</code>"),
        FakeResponse(text="<p>read:source not granted here</p>"),
    ]
    source = WikiJsSource(
        site_url="https://wiki.example",
        index_path=index,
        allowed_paths=["manual"],
        http_client=client,
    )

    with pytest.raises(WikiRagError, match="refresh incomplete"):
        source.refresh()

    assert index.read_text(encoding="utf-8") == "previous-good-index\n"


def test_ensure_fresh_swallows_refresh_error_and_keeps_index(tmp_path):
    index = tmp_path / "wiki.jsonl"
    index.write_text("previous-good-index\n", encoding="utf-8")
    client = Mock()
    client.post.return_value = page_list()
    source = WikiJsSource(
        site_url="https://wiki.example",
        index_path=index,
        allowed_paths=["*"],
        refresh_interval_seconds=60,
        http_client=client,
    )

    assert source.ensure_fresh(force=True) is None
    assert index.read_text(encoding="utf-8") == "previous-good-index\n"


def test_build_context_returns_empty_for_missing_index(tmp_path):
    rag = LocalWikiRag(str(tmp_path / "missing.jsonl"))
    assert rag.build_context("meshcore intro") == ""


def test_build_context_supports_old_json_array_indexes(tmp_path):
    index_file = tmp_path / "wiki.json"
    data = [
        {
            "path": "product/intro",
            "title": "Intro",
            "source_url": "https://wiki/s/product/intro",
            "chunks": ["This product is a long-range radio network."],
        }
    ]
    index_file.write_text(json.dumps(data), encoding="utf-8")
    rag = LocalWikiRag(str(index_file))
    context = rag.build_context("product radio")
    assert context.startswith("WIKI_REFERENCE_DATA_BEGIN")
    assert context.endswith("WIKI_REFERENCE_DATA_END")
    assert "product/intro" in context


def test_retrieval_prioritizes_headings_applies_thresholds_and_deduplicates(tmp_path):
    index_file = tmp_path / "wiki.jsonl"
    rows = [
        {
            "path": "manual/routing",
            "page_title": "Networking",
            "section_title": "Routing configuration",
            "section_index": 0,
            "content": "Set route_mode to flood.",
        },
        {
            "path": "manual/routing-copy",
            "page_title": "Copy",
            "section_index": 1,
            "content": "Set route_mode to flood.",
        },
        {
            "path": "manual/other",
            "page_title": "Other",
            "section_index": 2,
            "content": "Routing is mentioned once in unrelated prose.",
        },
    ]
    index_file.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    rag = LocalWikiRag(str(index_file), max_chunks=3, min_score=6, relative_score=0.55)

    result = rag.retrieve("routing configuration")

    assert result is not None
    assert result.matches[0].section.path == "manual/routing"
    assert result.context.count("Set route_mode to flood.") == 1
    assert "manual/other" not in result.context


def test_context_budget_keeps_complete_delimiters(tmp_path):
    index_file = tmp_path / "wiki.jsonl"
    row = {
        "path": "manual/radio",
        "page_title": "Radio",
        "section_title": "Radio configuration",
        "content": "radio configuration " * 100,
    }
    index_file.write_text(json.dumps(row) + "\n", encoding="utf-8")
    rag = LocalWikiRag(str(index_file), max_chars_per_chunk=500, max_context_chars=340)

    context = rag.build_context("radio configuration")

    assert len(context) <= 340
    assert context.startswith("WIKI_REFERENCE_DATA_BEGIN")
    assert context.endswith("WIKI_REFERENCE_DATA_END")


def test_corrupt_update_keeps_the_previously_loaded_index(tmp_path):
    index_file = tmp_path / "wiki.jsonl"
    row = {"path": "manual/radio", "page_title": "Radio", "content": "radio configuration settings"}
    index_file.write_text(json.dumps(row) + "\n", encoding="utf-8")
    rag = LocalWikiRag(str(index_file), min_score=3)
    assert "manual/radio" in rag.build_context("radio")
    old_mtime = index_file.stat().st_mtime_ns

    index_file.write_text("{invalid-json\n", encoding="utf-8")
    os.utime(index_file, ns=(old_mtime + 1_000_000, old_mtime + 1_000_000))

    assert "manual/radio" in rag.build_context("radio")


def test_aliases_are_applied_to_query_and_index(tmp_path):
    index_file = tmp_path / "wiki.jsonl"
    row = {"path": "manual/identity", "page_title": "Identity", "content": "Configure the callsign here."}
    index_file.write_text(json.dumps(row) + "\n", encoding="utf-8")
    rag = LocalWikiRag(str(index_file), aliases={"indicatif": "callsign"}, min_score=3)

    assert rag.retrieve("indicatif") is not None
