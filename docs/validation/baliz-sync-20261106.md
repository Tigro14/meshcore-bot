# Baliz / Tigro main integration — 2026-11-06

Baliz base: `50d2e1e79c193aaf4da528fd46fbe93df85f043b`.
Tigro main: `d040c5ad94c03b29b29ade6001e1084264adc46f` (merged PR #80).

## Integration

- Merge upstream history rather than duplicating the DM and clock changes already deployed in Baliz.
- Adopt upstream's recipient-resolver type annotation, isolated database test fixtures, generic clock/contact test data, DM/clock documentation and JavaScript regression-test workflow.
- Retain Baliz's assistant, closed catalogue, semantic router, sender eligibility, mentions, private-message behavior and service permissions byte-for-byte.
- Retain the public identity profile, privacy controls, advert counters, scope handling, entry-point analysis, region observations and migration numbering.
- Retain the French clock/delivery texts and the map's combined region, one-byte and clock indicators. Keep their pane ordering, permanent labels, popup layout, integrated sidebars and existing filters.
- Preserve the escaped graph title as a separate template literal before appending clock details, matching upstream's security regression check.
- This synchronization targets upstream `main`. The BBS/vigilance/region changes still on upstream `dev` are separate.

## Validation

Tests run locally with isolated test data. No production configuration, database or radio is changed, and no RF test message is transmitted.

- Targeted DM, clock evidence/replies, map clock API and XSS selection: **76 passed**.
- JavaScript regression tests: **30 passed**.
- HTMLHint passes on the four affected templates; ESLint reports no errors, with existing warnings.
- Log-injection check passes across **208 runtime files**, with no baseline or new violations.
- Ruff comparison: **55 existing findings** in both the base and candidate, with no new finding.
- Mypy comparison: **6 existing errors** in the base and **5** in the candidate; the upstream resolver annotation removes one. Remaining findings are in unchanged Baliz code.
- Full Python 3.13 reference run: **6,881 passed, 34 failed, 24 skipped**. Candidate: **6,882 passed, 33 failed, 24 skipped**. Failure-ID comparison finds **zero new failures**; the graph-title XSS regression check is repaired. The remaining failures are not presented as successful.

No deployment is performed by this GitHub synchronization. The production tag remains tied to the previously verified deployed source.
