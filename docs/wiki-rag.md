# Universal Wiki.js RAG

The optional Wiki.js RAG gives the local LLM a small set of relevant documentation
sections. It is lexical and deterministic: it does not require embeddings or a
vector database, and it contains no built-in wiki address or site-specific path.

## Configuration

Enable it in `[Llm_Command]`:

```ini
wiki_rag_enabled = true
wiki_site_url = https://wiki.example.org
wiki_locale = en
wiki_refresh_interval_seconds = 86400
wiki_verify_ssl = true
wiki_allowed_paths = handbook, radio/reference
wiki_rag_index_path = data/wiki_rag/wiki_pages.jsonl
wiki_rag_max_chunks = 2
wiki_rag_chunk_chars = 1400
wiki_rag_max_context_chars = 2400
wiki_rag_min_term_len = 3
wiki_rag_min_score = 6
wiki_rag_relative_score = 0.55
wiki_rag_stopwords =
wiki_rag_aliases =
```

`wiki_site_url` accepts any absolute HTTP or HTTPS Wiki.js base URL.
`wiki_allowed_paths` is mandatory for automatic collection. Each value includes an
exact page path and its descendants; `handbook` therefore includes
`handbook/install` but not `handbook-old`. Use `*` only when indexing every page
visible to the configured identity is intentional.

`wiki_locale` is optional. Leave it empty to request every locale visible to the
collector. Keep different sites or corpora in different index files.

Keep `wiki_verify_ssl = true`. Disabling certificate verification is intended only
for controlled testing with a self-signed internal instance.

## Authentication modes

The collector chooses its mode from the presence of an API key.

### Guest source access

Leave `wiki_api_key` empty. In Wiki.js, grant the **Guest** group access to the
selected paths and enable both page reading and source reading. The required Wiki.js
permissions are `read:pages` and `read:source`. The second permission is essential:
the collector reads the lossless source view at `/s/[locale/]<page-path>`. Without
it, Wiki.js may still display the rendered page in a browser, but the RAG refresh
fails because the source block is absent.

Guest source requests never contain an `Authorization` header.

### Wiki.js API key

Create a Wiki.js API key that can list and read the selected pages. Prefer exposing
it to the bot process instead of storing it in `config.ini`:

```text
MESHCORE_WIKI_API_KEY=replace-with-the-secret
```

The `wiki_api_key` setting is also supported when environment-based secret injection
is not available. In API mode, the collector uses Wiki.js GraphQL `pages.list` and
`pages.single`, with `Authorization: Bearer <key>`.

## Refresh lifecycle

Before an LLM question is processed, the bot checks the last successful refresh.
When the index is missing or older than `wiki_refresh_interval_seconds`, collection
runs in a worker thread so it does not block the bot event loop. A value of `0`
disables automatic refresh and permits an externally managed index.

The refresh performs these operations:

1. List the pages visible to the Guest identity or API key.
2. Keep only pages allowed by `wiki_allowed_paths`.
3. Fetch the original source of every selected page.
4. Split Markdown and AsciiDoc on headings, and convert HTML to readable text.
5. Split oversized prose while keeping fenced code blocks intact.
6. Write a complete JSONL index to a temporary file.
7. Atomically replace the old index and update its `.last-success` marker.

A partial, empty, invalid, HTTP or GraphQL result is never published. The last good
index remains available. Failed automatic attempts are rate-limited for 60 seconds.

The standalone collector uses the same implementation and is useful for validation
or an external scheduler:

```bash
python scripts/wikijs_rag_collect.py \
  --base-url https://wiki.example.org \
  --locale en \
  --allow-prefix handbook \
  --output data/wiki_rag/wiki_pages.jsonl
```

Pass `--token` for API mode, repeat `--allow-prefix`, or use `--allow-all` explicitly.

## Retrieval and LLM use

Each indexed section stores the page identifier, path, page title, heading, source
URL, update time, content type and original content. At query time the retriever:

1. normalizes case and accents while preserving technical punctuation;
2. removes a small built-in English/French stopword set plus configured stopwords,
   and expands configured aliases;
3. scores exact terms and adjacent phrases, prioritizing section title, page title
   and path over repeated matches in the body;
4. applies `wiki_rag_min_score` and `wiki_rag_relative_score`;
5. removes duplicate sections and retains at most `wiki_rag_max_chunks`;
6. builds a bounded reference block without cutting its closing delimiter.

When no section passes the thresholds, the normal LLM prompt is used. When a match
exists, the wiki prompt is isolated from conversation history, weather, topology and
other local context. The model is instructed to use only the selected excerpts, to
say when they are insufficient, and to preserve commands, identifiers, numbers,
URLs, paths, punctuation and hashtags exactly. Temperature is forced to zero for
that request. A conservative post-processing pass restores exact technical literals
and protects hashtags found in the selected source. Numeric sequences are never
changed by literal repair: a nearby identifier or frequency is not assumed to be
a typo. Retrieval also runs outside the bot event loop.

The index is reloaded only when its modification time changes. If a malformed index
appears, the previously loaded in-memory index is retained.

## Operational checks

If refresh fails in Guest mode, verify the selected Wiki.js path rules and both
`read:pages` and `read:source` for the Guest group. If listing works but one page
cannot be read, the complete refresh is rejected deliberately. If retrieval returns
no result, inspect the configured paths and relevance thresholds before lowering
them; a low threshold can inject unrelated documentation.

## Upgrading from the initial lexical RAG

Existing JSON arrays and JSONL indexes containing `content` or `chunks` remain
readable. Set `wiki_refresh_interval_seconds = 0` for an externally managed index.
The built-in defaults now select two sections of up to 1400 characters within a
2400-character total context; existing explicitly configured values take priority.

The site-specific `crawl_meshcore_bzh_wiki.py` and `crawl_meshcore_wiki.py` wrappers
are replaced by `wikijs_rag_collect.py`. Update scheduled invocations to supply
`--base-url`, the appropriate `--locale`, and explicit `--allow-prefix` values.
There is no longer a default list of MeshCore page prefixes or a default French
locale. `--max-chars` remains an alias for `--max-section-chars`; oversized fenced
code blocks are retained intact in the index.

Collection is checked on demand, not on a background timer. The request that
triggers a stale refresh waits for collection, although other event-loop work can
continue. For large corpora, prebuild the index with the standalone collector and
use an external schedule. Use separate index paths for separate source scopes.
