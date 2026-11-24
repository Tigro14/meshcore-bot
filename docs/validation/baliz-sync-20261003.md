# Baliz / Tigro integration — 2026-10-03

Merged Tigro main `0fe71a0` into Baliz `9ff3afc`. Production module, translation and dependency-manifest hashes matched Baliz before integration. The only textual merge conflict was in CHANGELOG.md; both histories were retained.

Validation with Python 3.12.14, same environment for baseline and candidate:

- Baseline: 4,925 passed, 59 failed, 34 skipped, 7 deselected.
- Candidate: 5,378 passed, 39 failed, 34 skipped, 7 deselected.
- Exact failure-ID comparison: zero new failures; 20 upstream graph failures resolved. Remaining failures predate this merge, including legacy assistant fixtures, clock expectations, AQI and the semantic-timeout schema entry.
- Current Baliz catalogue, dispatch, first-hop location, relay usage/conversation and connectivity tests: 82 passed.
- Ruff: 30 findings versus 31 before; mypy: the same three errors in Baliz-specific near_command and assistant/llm_service.
- git diff --check passed.
- Candidate compile and validation against production config passed on LXC103 Python 3.13.5. Existing config warnings: legacy public_enabled keys and undocumented cpu_temp_threshold.
- Installed meshcore 2.3.14 satisfies the new minimum; pip check passed.

Deployment preserves /etc/meshcore-bot/config.ini and runtime data. Backup location: /root/baliz-tigro-backup-20261003.
