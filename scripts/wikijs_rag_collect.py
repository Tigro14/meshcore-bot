#!/usr/bin/env python3
"""Build a local lexical-RAG index from any configured Wiki.js instance."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from modules.wiki_rag import WikiJsSource  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect a Wiki.js corpus into an atomic local JSONL index.")
    parser.add_argument("--base-url", required=True, help="Wiki.js base URL, for example https://wiki.example.org")
    parser.add_argument(
        "--token",
        default=os.getenv("MESHCORE_WIKI_API_KEY", os.getenv("WIKIJS_TOKEN", "")),
        help="optional Wiki.js API token (prefer MESHCORE_WIKI_API_KEY)",
    )
    parser.add_argument("--locale", default="", help="optional Wiki.js locale used by pages.list")
    parser.add_argument("--output", default="data/wiki_rag/wiki_pages.jsonl", help="destination JSONL index")
    parser.add_argument(
        "--allow-prefix",
        action="append",
        default=[],
        help="allowed page path prefix; repeat for multiple prefixes",
    )
    parser.add_argument(
        "--allow-all",
        action="store_true",
        help="explicitly include every page visible to the configured identity",
    )
    parser.add_argument("--max-section-chars", "--max-chars", type=int, default=4000, help="maximum stored prose section size (code blocks stay intact)")
    parser.add_argument("--timeout", type=float, default=20.0, help="HTTP timeout in seconds")
    parser.add_argument("--insecure", action="store_true", help="disable TLS verification (not recommended)")
    args = parser.parse_args()

    prefixes = ["*"] if args.allow_all else args.allow_prefix
    if not prefixes:
        parser.error("configure at least one --allow-prefix, or explicitly use --allow-all")

    try:
        source = WikiJsSource(
            site_url=args.base_url,
            index_path=args.output,
            allowed_paths=prefixes,
            locale=args.locale,
            api_key=args.token,
            refresh_interval_seconds=0,
            timeout=args.timeout,
            verify_ssl=not args.insecure,
            max_section_chars=args.max_section_chars,
        )
        result = source.refresh()
    except Exception as exc:
        print(f"error: collection failed: {exc}", file=sys.stderr)
        return 1

    print(
        "ok: "
        f"listed={result.listed} selected={result.selected} fetched={result.fetched} "
        f"sections={result.sections} output={result.index_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
