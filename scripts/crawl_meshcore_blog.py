#!/usr/bin/env python3
"""
Crawler dédié pour la catégorie Meshcore de serveurperso.in.
Sortie: JSONL compatible LocalWikiRag.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import deque
from html import unescape
from urllib.parse import urljoin, urlparse, urldefrag

import requests

from wikijs_rag_collect import split_chunks


def _strip_html(text: str) -> str:
    text = re.sub(r"(?is)<(script|style|noscript).*?>.*?</\1>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return " ".join(unescape(text).split()).strip()


def _extract_links(html: str, base_url: str) -> list[str]:
    links: list[str] = []
    for href in re.findall(r"""(?is)href\s*=\s*["']([^"']+)["']""", html or ""):
        url = urljoin(base_url, href.strip())
        url, _ = urldefrag(url)
        links.append(url)
    return links


def _is_article_url(url: str, category_path: str, host: str) -> bool:
    parsed = urlparse(url)
    if parsed.netloc.lower() != host:
        return False
    path = parsed.path or ""
    if not path.startswith("/archives/"):
        return False
    if path.startswith(category_path.rstrip("/") + "/"):
        return False
    if "/category/" in path:
        return False
    if re.search(r"/page/\d+/?$", path):
        return False
    return True


def _extract_article_body(html: str) -> str:
    patterns = [
        r"""(?is)<article\b[^>]*>(.*?)</article>""",
        r"""(?is)<div\b[^>]*class=["'][^"']*(?:entry-content|post-content|td-post-content)[^"']*["'][^>]*>(.*?)</div>""",
        r"""(?is)<main\b[^>]*>(.*?)</main>""",
    ]
    for pattern in patterns:
        match = re.search(pattern, html or "")
        if match:
            body = _strip_html(match.group(1))
            if body:
                return body
    return _strip_html(html or "")


def _extract_title(html: str, fallback: str) -> str:
    for pattern in [
        r"""(?is)<meta\s+property=["']og:title["']\s+content=["']([^"']+)["']""",
        r"""(?is)<h1\b[^>]*>(.*?)</h1>""",
        r"""(?is)<title\b[^>]*>(.*?)</title>""",
    ]:
        match = re.search(pattern, html or "")
        if match:
            title = _strip_html(match.group(1))
            if title:
                return title
    return fallback


def _extract_updated_at(html: str) -> str | None:
    for pattern in [
        r"""(?is)<time\b[^>]*datetime=["']([^"']+)["']""",
        r"""(?is)<meta\s+property=["']article:modified_time["']\s+content=["']([^"']+)["']""",
        r"""(?is)<meta\s+property=["']article:published_time["']\s+content=["']([^"']+)["']""",
    ]:
        match = re.search(pattern, html or "")
        if match:
            value = match.group(1).strip()
            if value:
                return value
    return None


def collect_blog_to_jsonl(
    *,
    category_url: str,
    output: str,
    max_chars: int,
    timeout: float,
    verify_ssl: bool,
    max_category_pages: int = 20,
) -> tuple[int, int, int, str]:
    session = requests.Session()
    queue: deque[str] = deque([category_url])
    visited_category_pages: set[str] = set()
    article_urls: list[str] = []
    seen_articles: set[str] = set()

    parsed_category = urlparse(category_url)
    host = parsed_category.netloc.lower()
    category_path = parsed_category.path.rstrip("/")

    while queue and len(visited_category_pages) < max(1, max_category_pages):
        page_url = queue.popleft()
        if page_url in visited_category_pages:
            continue
        visited_category_pages.add(page_url)
        response = session.get(page_url, timeout=timeout, verify=verify_ssl)
        response.raise_for_status()
        html = response.text or ""
        links = _extract_links(html, page_url)

        for link in links:
            parsed = urlparse(link)
            if parsed.netloc.lower() != host:
                continue
            path = parsed.path or ""
            if path.startswith(category_path.rstrip("/") + "/page/"):
                if link not in visited_category_pages:
                    queue.append(link)
                continue
            if _is_article_url(link, category_path, host) and link not in seen_articles:
                seen_articles.add(link)
                article_urls.append(link)

    output_path = os.path.abspath(output)
    output_dir = os.path.dirname(output_path) or "."
    os.makedirs(output_dir, exist_ok=True)

    fetched = 0
    with open(output_path, "w", encoding="utf-8") as out:
        for article_url in article_urls:
            response = session.get(article_url, timeout=timeout, verify=verify_ssl)
            response.raise_for_status()
            html = response.text or ""
            parsed = urlparse(article_url)
            path = (parsed.path or "").strip("/") or article_url
            title = _extract_title(html, path.split("/")[-1])
            content = _extract_article_body(html)
            if not content:
                continue
            record = {
                "id": article_url,
                "path": path,
                "title": title,
                "updated_at": _extract_updated_at(html),
                "source_url": article_url,
                "content": content,
                "chunks": split_chunks(content, max(200, max_chars)),
            }
            out.write(json.dumps(record, ensure_ascii=False) + "\n")
            fetched += 1

    return len(visited_category_pages), len(article_urls), fetched, output_path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Crawl la catégorie Meshcore de serveurperso.in au format JSONL RAG."
    )
    parser.add_argument(
        "--category-url",
        default="https://serveurperso.in/archives/category/meshcore",
        help="URL de la catégorie à crawler",
    )
    parser.add_argument(
        "--output",
        default="data/wiki_rag/serveurperso_meshcore.jsonl",
        help="Fichier JSONL de sortie",
    )
    parser.add_argument("--max-chars", type=int, default=700, help="Taille max par chunk")
    parser.add_argument("--timeout", type=float, default=20.0, help="Timeout HTTP")
    parser.add_argument("--max-category-pages", type=int, default=20, help="Maximum de pages catégorie à explorer")
    parser.add_argument("--insecure", action="store_true", help="Désactive la vérification TLS")
    args = parser.parse_args()

    try:
        pages, listed, fetched, output_path = collect_blog_to_jsonl(
            category_url=args.category_url,
            output=args.output,
            max_chars=args.max_chars,
            timeout=args.timeout,
            verify_ssl=not args.insecure,
            max_category_pages=args.max_category_pages,
        )
    except Exception as exc:
        print(f"error: crawl failed: {exc}", file=sys.stderr)
        return 1

    print(f"ok: pages={pages} listed={listed} fetched={fetched} output={output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
