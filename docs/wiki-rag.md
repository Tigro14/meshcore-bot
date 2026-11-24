# Universal Wiki.js RAG

The optional Wiki.js RAG gives the local LLM a small set of relevant documentation
sections. It is lexical and deterministic: it does not require embeddings or a
vector database, and it contains no built-in wiki address or site-specific path.

## Configuration

Enable it in `[Rag_Service]`:

```ini
[Rag_Service]
enabled = true
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

The RAG settings belong to this shared configuration section; endpoint, model,
conversation prompt and history stay in `[Llm_Command]`. This does not create a
public `rag` command or a background service plugin. The `wiki` assistant route
uses this configuration and still needs the LLM for synthesis.

For migration, move every `wiki_*` setting from `[Llm_Command]` into
`[Rag_Service]` and rename `wiki_rag_enabled` to `enabled`. Old settings remain
readable when the corresponding new key is absent. New values take precedence,
including explicit `false` and empty strings. `MESHCORE_WIKI_API_KEY` retains
precedence over the configured API key. Remove old keys after migration to avoid
ambiguity. Merely adding the example's `enabled = false` explicitly disables RAG.

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
4. discounts link directories (at least two standalone links making up 60% of
   meaningful lines) to one quarter of their score; ordinary prose with links
   and fenced code are not classified as directories;
5. ranks pages by their strongest title/path/section-title evidence, not by page
   length. When the leading page has at least 12 metadata points, exceeds the
   runner-up by more than 25%, and the query has at least two distinct search
   terms, its relevant sections are considered first. Otherwise global section
   ranking remains in place, allowing answers from multiple pages;
6. applies `wiki_rag_min_score` and `wiki_rag_relative_score`;
7. removes duplicate sections and retains at most `wiki_rag_max_chunks`;
8. builds a bounded reference block without cutting its closing delimiter.

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


### Page selection and scope

Page priority does not bypass relevance thresholds or increase the configured
context budget. Lower-ranked pages can still fill remaining slots. This restores
the benefit of narrowing a question to its relevant document without encoding
site URLs, equipment names or page paths in the retriever. The heuristics use
metadata and document structure; they are not a semantic classifier. The default
two sections cannot guarantee coverage of every step in a long procedure.

On the MeshCore Bretagne corpus used for validation, “Bonjour, comment configurer
un répéteur ?” now selects the repeater introduction and its first settings step,
instead of the introduction and a navigation directory. This is a retrieval
validation, not a claim that LLM synthesis errors are fixed. No configuration
budget changes are required.


### Procedure-aware retrieval

Broad setup questions (French/English intent vocabulary, with no device-specific
rules) can expand an unambiguously identified page into its numbered steps,
parameter tables, prerequisites, warnings and verification sections. The subject
must match page metadata; a question naming an additional parameter stays targeted.
Specific terms absent from page metadata are searched in section headings/content
so repeated page titles do not outweigh the requested parameter.

`Rag_Service` adds `wiki_procedure_max_sections = 6` and
`wiki_procedure_max_context_chars = 6000`. These replace the ordinary excerpt
limits only for detected broad procedures. Steps are kept complete and in source
order; if a step cannot fit, expansion stops and the context is explicitly marked
incomplete. No setting, equipment name or regional value is hardcoded. Detection
is deliberately conservative and depends on useful page headings, not a semantic
classifier. Unstructured procedures can still use ordinary lexical retrieval.

`wiki_response_max_tokens = 384` controls wiki generation independently of
conversational `Llm_Command.max_tokens`. The wiki prompt prioritizes exact technical
values and commands, conditions, save/check steps and explicit adaptation of example
values. Existing radio pagination still limits delivery; this token budget does not
guarantee that an entire procedure fits one response. Conflicting source instructions
must be reported, not silently corrected. Larger contexts may increase model latency.

Optional HTML `details` explanations are omitted in procedure contexts unless
they contain recognized caution/condition vocabulary. Main steps and code blocks
remain intact. This compaction is conservative for French/English documentation.
