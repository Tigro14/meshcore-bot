#!/usr/bin/env python3
"""
Collect Wiki.js pages into a local JSONL index for lexical RAG.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from urllib.parse import quote, urljoin

import requests

GRAPHQL_LIST_PAGES = """
query ListPages($locale: String!, $limit: Int!) {
  pages {
    list(locale: $locale, limit: $limit) {
      id
      path
      title
      updatedAt
    }
  }
}
"""


def _looks_like_html(text: str) -> bool:
    head = (text or "").lstrip().lower()[:300]
    return "<!doctype html" in head or "<html" in head or "<body" in head


def _strip_html(text: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return " ".join(text.split()).strip()


def split_chunks(markdown: str, max_chars: int) -> list[str]:
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", markdown) if p.strip()]
    chunks: list[str] = []
    current = ""
    for para in paragraphs:
        candidate = f"{current}\n\n{para}" if current else para
        if len(candidate) <= max_chars:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = ""
        if len(para) <= max_chars:
            current = para
        else:
            start = 0
            while start < len(para):
                part = para[start : start + max_chars].strip()
                if part:
                    chunks.append(part)
                start += max_chars
    if current:
        chunks.append(current)
    return chunks


def graphql_list_pages(base_url: str, token: str, locale: str, timeout: float, verify_ssl: bool) -> list[dict]:
    endpoint = urljoin(base_url.rstrip("/") + "/", "graphql")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    payload = {"query": GRAPHQL_LIST_PAGES, "variables": {"locale": locale, "limit": 10000}}
    response = requests.post(endpoint, json=payload, headers=headers, timeout=timeout, verify=verify_ssl)
    response.raise_for_status()
    data = response.json()
    pages = data.get("data", {}).get("pages", {}).get("list", [])
    if not isinstance(pages, list):
        return []
    return [p for p in pages if isinstance(p, dict) and p.get("path")]


def fetch_markdown(
    base_url: str,
    page_path: str,
    token: str,
    timeout: float,
    verify_ssl: bool,
    locale: str = "fr",
) -> tuple[str, str]:
    clean_path = page_path.strip("/")
    encoded_path = quote(clean_path)
    locale = (locale or "").strip("/")
    candidates = [
        f"/s/{encoded_path}?format=md",
        f"/s/{encoded_path}/raw",
        f"/s/{encoded_path}",
        f"/s/{locale}/{encoded_path}?format=md" if locale else "",
        f"/s/{locale}/{encoded_path}/raw" if locale else "",
        f"/s/{locale}/{encoded_path}" if locale else "",
        f"/{locale}/{encoded_path}?format=md" if locale else "",
        f"/{locale}/{encoded_path}" if locale else "",
    ]
    attempted: list[str] = []
    for candidate in candidates:
        if not candidate:
            continue
        url = urljoin(base_url.rstrip("/") + "/", candidate.lstrip("/"))
        attempted.append(url)
        headers = {"Authorization": "Bearer " + token} if token else None
        response = requests.get(url, headers=headers, timeout=timeout, verify=verify_ssl)
        if response.status_code != 200:
            continue
        text = (response.text or "").strip()
        if not text:
            continue
        if _looks_like_html(text):
            text = _strip_html(text)
        if text:
            return text, url
    raise RuntimeError(
        f"Unable to fetch markdown for path '{page_path}'. Tried: {', '.join(attempted)}"
    )


def allowed_path(page_path: str, prefixes: list[str]) -> bool:
    normalized = page_path.strip("/").lower()
    return any(normalized.startswith(prefix.strip("/").lower()) for prefix in prefixes)


def collect_to_jsonl(
    *,
    base_url: str,
    token: str,
    locale: str,
    output: str,
    prefixes: list[str],
    max_chars: int,
    timeout: float,
    verify_ssl: bool,
) -> tuple[int, int, int, str]:
    pages = graphql_list_pages(base_url, token, locale, timeout, verify_ssl)
    selected = [page for page in pages if allowed_path(str(page.get("path", "")), prefixes)]
    output_path = os.path.abspath(output)
    output_dir = os.path.dirname(output_path) or "."
    os.makedirs(output_dir, exist_ok=True)

    fetched = 0
    with open(output_path, "w", encoding="utf-8") as out:
        for page in selected:
            page_path = str(page.get("path", "")).strip()
            title = str(page.get("title", "")).strip()
            if not page_path:
                continue
            try:
                markdown, source_url = fetch_markdown(base_url, page_path, token, timeout, verify_ssl, locale)
            except Exception as exc:
                print(f"warn: skip {page_path}: {exc}", file=sys.stderr)
                continue
            record = {
                "id": page.get("id"),
                "path": page_path,
                "title": title,
                "updated_at": page.get("updatedAt"),
                "source_url": source_url,
                "content": markdown,
                "chunks": split_chunks(markdown, max(200, max_chars)),
            }
            out.write(json.dumps(record, ensure_ascii=False) + "\n")
            fetched += 1
    return len(pages), len(selected), fetched, output_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect Wiki.js pages for local lexical RAG.")
    parser.add_argument("--base-url", required=True, help="Wiki.js base URL (e.g. https://wiki.example.org)")
    parser.add_argument("--token", default=os.getenv("WIKIJS_TOKEN", ""), help="Wiki.js API token")
    parser.add_argument("--locale", default="fr", help="Wiki.js locale to query via GraphQL")
    parser.add_argument("--output", default="data/wiki_rag/wiki_pages.jsonl", help="Output JSONL file path")
    parser.add_argument(
        "--allow-prefix",
        action="append",
        default=None,
        help="Allowed page path prefix (repeat option for multiple values)",
    )
    parser.add_argument("--max-chars", type=int, default=700, help="Max chars per chunk")
    parser.add_argument("--timeout", type=float, default=20.0, help="HTTP timeout in seconds")
    parser.add_argument("--insecure", action="store_true", help="Disable TLS verification")
    args = parser.parse_args()

    allow_prefixes = args.allow_prefix or ["configuration/", "demarrer/", "ressources/"]

    verify_ssl = not args.insecure
    try:
        listed, selected, fetched, output_path = collect_to_jsonl(
            base_url=args.base_url,
            token=args.token,
            locale=args.locale,
            output=args.output,
            prefixes=allow_prefixes,
            max_chars=args.max_chars,
            timeout=args.timeout,
            verify_ssl=verify_ssl,
        )
    except Exception as exc:
        print(f"error: GraphQL page listing failed: {exc}", file=sys.stderr)
        return 1

    print(f"ok: listed={listed} selected={selected} fetched={fetched} output={output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
