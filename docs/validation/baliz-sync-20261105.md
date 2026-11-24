# Baliz / Tigro integration — 2026-11-05

Baliz base: `90666d8a5f1c3f9afca29b973a77f1b2d887f3cc`.
Tigro main merged: `b4a85ebcef872359ec7b49b1fc767e26b8e26b0d` (includes PR #76).

## Integration decisions

- Adopt the upstream maintainability refactor, local web assets, dashboard snapshots, Realtime tape, radio UI, channel occupancy, Météo-France vigilance capability, weather provider corrections, transport fixes and lifecycle changes.
- Keep Baliz's closed semantic catalogue, router examples, dispatcher, private-message activation, mentions, conversation isolation, Wiki retrieval and answer policy. These files are retained from the deployed base. No keyword intent overrides are introduced.
- Keep the shared LLM engine behind the existing Baliz command wrapper. Port the optional generation ceiling of 1,024 tokens, retaining configured values, the 300-second timeout and the configured Baliz identity. Upstream's replacement monolithic command and model-role instruction are not substituted for Baliz's assistant.
- Separate internal capability authorization from direct-command enable switches after upstream moves the enable checks into BaseCommand. Preserve channel, DM, cooldown and admin checks.
- Preserve first incoming-hop positioning, compact integrated entry-point/configuration sidebars, warning colours, persistent labels and day/night basemaps. Adapt the mesh API enrichments to the new file structure.
- Preserve rolling deduplicated advert counts, including sorting before pagination and totals over the entire filtered cohort. Keep the dashboard's assistant-function usage card in the new snapshot interface.
- Keep transient Open-Meteo retries and city grounding. Update only the request/log reference snapshots for retries; weather result snapshots do not change.
- Retain publication consent and the readable metadata-only log policy. Upstream's integrated PR implementation is used in its new live-stream and channel modules.
- Existing Baliz migration numbers 30 (assistant usage), 31 (advert rollup), 32 (scope) and 33 (publication consent) are immutable. Append occupancy tables as migration 34. Test an actual version-33 upgrade, consent/usage preservation and a repeated migration run.
- Do not change production configuration, private plugins, local data, channel publication choices, endpoints, scopes or scheduled transmissions. New optional services remain subject to the existing explicit enable switches.

## Verification

Validation runs locally, never in the production LXC. No radio test messages are sent.

Baseline, Python 3.13.18: 5,603 passed, 46 failed, 17 skipped, 7 deselected. Existing failures include old LLM-wrapper/assistant fixtures and clock expectations; they are compared by test ID rather than presented as successful.

Final validation results are recorded below after the complete comparison.

- Candidate full suite: **6,754 passed, 44 failed, 17 skipped, 8 deselected**. Failure-ID comparison: **zero new failures**; two stale baseline fixtures were corrected. The additional deselection is a local socket-binding test, run separately with local-port permissions: passed.
- Final release checks after preserving provider place labels for captured weather calls: **235 passed**, covering real provider formatting, weather grounding, hidden-channel publication, logs, mentions and upgrade compatibility.
- Refactor compatibility / access / contacts / provider selection: **151 passed**.
- Frontend: HTMLHint passed (32 templates), ESLint passed with existing warnings and no errors, contacts JavaScript tests **11 passed**.
- Scoped Python lint, compilation, diff whitespace checks and log-injection check passed (204 scanned files, zero baseline/new log-injection violations).
- Closed catalogue, semantic router, dispatcher, ASK command, Wiki service, first-hop origin resolver, entry-point analysis and `config.ini.baliz` are byte-for-byte identical to the deployed base.
- Production preflight: deployed source matched the known base, schema 33, four published channels, no occupancy tables. Current config and visibility digests are held outside the repository for the deployment preservation checks. New dependency installation completed with `pip check` passing.
