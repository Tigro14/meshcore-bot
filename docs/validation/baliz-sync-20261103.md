# Baliz / Tigro integration — 2026-11-03

Base: Baliz `6c7e68dcb91a789f42322fe2ef9e7c74bcff21f9`.
Upstream merged: Tigro main `7af0ff513875c45be8204af984ba50142e12ca57`.

## Included improvements

- Dashboard volume and sparkline for single-byte repeater adverts; restored daily region traffic chart.
- MeshCore contact QR URI corrections and lazy repeater-manager geocoding lookup.
- Per-message flood-scope verdict persistence and full-public-key mesh distance handling, with an explicit offline repair script.
- Local Web Viewer test-DM panel, using captured responses and a dummy bot with no radio connection.
- Optional primary LLM endpoint, cached health check and fallback. Authentication is restricted to the configured primary; retries share one time budget. Optional model-context budgeting is applied to local context, not the isolated Wiki reference prompt.
- Read-only filesystem monitoring and Wiki write-failure reporting.

## Baliz compatibility decisions

`config.ini.baliz`, the closed catalogue/router/dispatcher, the SQL-free assistant boundary, mentions, private-message activation, regional identity, default location, first-hop positioning, entry-point selection, warning colours and the main mesh UI are unchanged from the base.

Baliz's `LlmService` remains outside command routing. Applicable upstream LLM features were ported there rather than replacing its command wrapper with the upstream monolith. The upstream file contains unresolved merge text and duplicate definitions, so copying it verbatim would break startup. Hidden `reasoning_content` is never sent on the radio.

Baliz's RAG service section, relevance ranking, full procedure handling, response tone and technical literals are preserved. The upstream minimum-matching-term filter is available explicitly; its default remains 1 to preserve existing retrieval. The new reboot semaphore requires explicit `reboot_on_erofs=true`; it is not activated by the upgrade. No endpoint switch occurs without configuration. Automatic context sizing is only enabled when a model context size or a positive explicit budget is configured.

Migration 30 remains Baliz assistant-function usage. Upstream migrations are assigned 31 (single-byte advert rollup) and 32 (message scope) so existing Baliz installations execute them. A regression test upgrades a version-30 database and checks that assistant usage rows survive.

## Validation

Python 3.12.14, identical local environment, MQTT/live-radio tests excluded:

- Base: 5,453 passed, 50 failed, 34 skipped, 7 deselected.
- Candidate full-suite run: 5,489 passed, 50 failed, 34 skipped, 7 deselected.
- Failure-ID comparison: **zero new failures**, exactly the same 50 failures as the base.
- Final assistant, mentions, entry points, contacts and optional-feature compatibility selection: 105 passed.
- Wiki/commands/dashboard/migrations/watchdog selection: 296 passed.
- Contacts/dashboard/migrations/watchdog compatibility selection: 154 passed.
- Compilation of modules and scripts and `git diff --check` passed.

The full suite has pre-existing failures, including legacy LLM command-wrapper expectations, stale clock tests and older assistant fixtures. These are not reported as passing.

Run comprehensive validation on a workstation or a separate test container. A production bot container with limited memory is unsuitable for concurrent full-suite jobs.

This integration updates GitHub only. It does not replace production code, configuration, databases or runtime indexes.
