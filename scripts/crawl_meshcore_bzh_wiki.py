#!/usr/bin/env python3
"""
Crawler dédié pour la section Meshcore de https://wiki.meshcore.bzh
Sortie: JSONL compatible LocalWikiRag.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from wikijs_rag_collect import allowed_path, fetch_markdown, graphql_list_pages, split_chunks


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Crawl la section Meshcore du Wiki.js meshcore.bzh au format JSONL RAG."
    )
    parser.add_argument("--base-url", default="https://wiki.meshcore.bzh", help="URL de base Wiki.js")
    parser.add_argument("--locale", default="fr", help="Locale Wiki.js")
    parser.add_argument("--token", default=os.getenv("WIKIJS_TOKEN", ""), help="Token API Wiki.js (optionnel)")
    parser.add_argument(
        "--prefix",
        action="append",
        default=None,
        help="Préfixe(s) de chemin autorisé(s), ex: meshcore/",
    )
    parser.add_argument(
        "--output",
        default="data/wiki_rag/meshcore_bzh_wiki_pages.jsonl",
        help="Fichier JSONL de sortie",
    )
    parser.add_argument("--max-chars", type=int, default=700, help="Taille max par chunk")
    parser.add_argument("--timeout", type=float, default=20.0, help="Timeout HTTP")
    parser.add_argument("--insecure", action="store_true", help="Désactive la vérification TLS")
    args = parser.parse_args()

    verify_ssl = not args.insecure
    prefixes = args.prefix or ["meshcore/"]

    try:
        pages = graphql_list_pages(args.base_url, args.token, args.locale, args.timeout, verify_ssl)
    except Exception as exc:
        print(f"error: GraphQL list failed: {exc}", file=sys.stderr)
        return 1

    selected = [page for page in pages if allowed_path(str(page.get("path", "")), prefixes)]

    output_path = os.path.abspath(args.output)
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    fetched = 0
    with open(output_path, "w", encoding="utf-8") as out:
        for page in selected:
            page_path = str(page.get("path", "")).strip()
            if not page_path:
                continue
            try:
                markdown, source_url = fetch_markdown(args.base_url, page_path, args.token, args.timeout, verify_ssl)
            except Exception as exc:
                print(f"warn: skip {page_path}: {exc}", file=sys.stderr)
                continue
            record = {
                "id": page.get("id"),
                "path": page_path,
                "title": str(page.get("title", "")).strip(),
                "updated_at": page.get("updatedAt"),
                "source_url": source_url,
                "content": markdown,
                "chunks": split_chunks(markdown, max(200, args.max_chars)),
            }
            out.write(json.dumps(record, ensure_ascii=False) + "\n")
            fetched += 1

    print(f"ok: listed={len(pages)} selected={len(selected)} fetched={fetched} output={output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
