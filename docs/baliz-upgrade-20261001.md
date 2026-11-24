# Baliz / Tigro synchronization — 2026-10-01

The LXC103 production code was compared with origin/baliz (9404ea5) before
merging Tigro main (9666df6). Eighteen production Python files were missing
or different: closed-catalog ASK routing, internal command adapters,
first-hop proximity, concise conversation, active-node counting and graph
connectivity ranking. They were preserved first, with their regression tests.

The upstream merge includes:
- 3fb97a4: optional battery telemetry, retention, migration 28 and web badges.
- 6604f00: optional startup flood scope.
- 9666df6: country filter already present in Baliz; merge produces no duplicate.

No upstream assistant replacement is introduced. The catalog remains closed
and model-selected. Existing service permission checks remain enforced.
The public profile reflects the running LLM timeout (300 s); the ASK settings
schema and profile use the interpreter's existing 45 s default. The live RAG
index path is deployment-specific and is not substituted into the public profile.

## Validation

The same selected Python suites were run on preserved production code before
and after the merge:
- Before: 549 passed, 56 failed, 2 skipped.
- After: 596 passed, 56 failed, 2 skipped.
- Exact failing test-ID sets match; 47 new upstream tests pass.
- The recent network/catalog/first-hop/conversation tests pass.
- Contacts JavaScript tests: 11 passed, including battery badges.
- Migration tested against a SQLite backup of production: integrity_check=ok,
  schema version 28, 538 contacts before and after.

The 56 existing failures cover obsolete ASK expectations and existing clock /
multibyte graph discrepancies. This upgrade does not claim a clean full suite.
The previously recorded 7/10 human routing evaluation remains a known limitation;
this synchronization does not implement the proposed two-stage interpreter.

Battery monitoring remains off unless configured. Startup flood scope remains
unset unless configured. Production configuration and model are not replaced by
the example/profile files during deployment.
