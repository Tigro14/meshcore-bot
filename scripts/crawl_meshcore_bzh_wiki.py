#!/usr/bin/env python3
"""
Crawler dédié pour la section Meshcore de https://wiki.meshcore.bzh
Sortie: JSONL compatible LocalWikiRag.
"""

from __future__ import annotations

import argparse
import os
import sys

from wikijs_rag_collect import collect_to_jsonl


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Crawl la section Meshcore du Wiki.js meshcore.bzh au format JSONL RAG."
    )
    parser.add_argument("--base-url", default="https://wiki.meshcore.bzh", help="URL de base Wiki.js")
    parser.add_argument("--locale", default="fr", help="Locale Wiki.js")
    parser.add_argument("--token", default=os.getenv("WIKIJS_TOKEN", ""), help="Token API Wiki.js (optionnel)")
    parser.add_argument("--prefix", action="append", default=None, help="Préfixe(s) autorisé(s), ex: meshcore/")
    parser.add_argument(
        "--output",
        default="data/wiki_rag/meshcore_bzh_wiki_pages.jsonl",
        help="Fichier JSONL de sortie",
    )
    parser.add_argument("--max-chars", type=int, default=700, help="Taille max par chunk")
    parser.add_argument("--timeout", type=float, default=20.0, help="Timeout HTTP")
    parser.add_argument("--insecure", action="store_true", help="Désactive la vérification TLS")
    args = parser.parse_args()

    try:
        listed, selected, fetched, output_path = collect_to_jsonl(
            base_url=args.base_url,
            token=args.token,
            locale=args.locale,
            output=args.output,
            prefixes=args.prefix or ["meshcore/"],
            max_chars=args.max_chars,
            timeout=args.timeout,
            verify_ssl=not args.insecure,
        )
    except Exception as exc:
        print(f"error: crawl failed: {exc}", file=sys.stderr)
        return 1

    print(f"ok: listed={listed} selected={selected} fetched={fetched} output={output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
