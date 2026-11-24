# Baliz: consistent clock evidence and truthful sync status

Base: `fd3ab783ce97820d68cbb88e1fb21e2e1e3b980a`.
Scope: local branch and LXC103 only; no GitHub publication.

## Changes

`modules/clock_observations.py` supplies the scheduler, Time, Contacts and dashboard with the same latest received clock sample. Message receipt timestamps and synchronization log timestamps are SQLite UTC; historical advertisement receipt timestamps use the bot's local timezone. Explicit offsets and Unix epochs are supported. The latest message/advert is chosen after conversion to epochs, preventing one-hour errors and incorrect source selection. Ambiguous contact names are not assigned a clock sample.

An ACK or a fresh reply confirms delivery only. Time and Contacts separate that transport result from clock evidence:

- A confirmed send without a newer clock sample is **Horloge à vérifier**.
- A later fresh measurement within the existing `min_sync_drift` threshold is **À l’heure**.
- A larger measured drift is a warning up to the existing dashboard threshold, then an error.
- An explicit firmware rejection is **Commande refusée**, until superseded by new evidence.
- Pre-2000 / beyond-2099 device dates are presented as **Horloge non réglée**, without multi-decade durations. Their numeric difference remains available internally so the scheduler still attempts correction.
- Old measurements are dated and marked stale; an old failed transmission does not override a valid measured clock.

Both pages display the observation date/source. The Contacts last-heard column is labelled **Dernière annonce**, matching what it records. Time's former Last Sync column is **Dernier envoi**, and its history describes reception rather than claiming synchronization succeeded. UTC log dates are serialized with an explicit offset and parsed correctly in the browser.

The scheduler uses the same measurement and existing observation window, and logs its source/date. This fixes the observed unnecessary Lanmeur synchronization caused by reading a local advert receipt as UTC. Existing ACK/retry policy, companion-only assistant access, clock target opt-ins, configuration, privacy preferences and database schema remain unchanged. No historical rows are rewritten.

## Validation

- 1,253 targeted Python 3.13 checks passed; two checks skipped. This includes clock evidence, scheduler, APIs, receive handling, DM contacts/ACKs, companion eligibility, privacy and closed-catalog routing.
- Two unrelated legacy web tests were deselected after reproducing the same failures on the unchanged base: the allowed-table whitelist fixture and the old standalone entry-points link expectation.
- 234 focused clock/scheduler checks passed on Python 3.10.
- 16 JavaScript tests passed, including the actual Time/Contacts rendering, French-timezone UTC parsing, Concarneau's old unset clock after ACK, and Trédudon's valid clock despite an older failed delivery.
- Lint, compilation, log-injection scanning, HTMLHint and ESLint passed (ESLint warnings only).

Tests reproduce the observed data: Lanmeur 27 seconds; Trédudon 9 seconds rather than 3,591; Concarneau's invalid old timestamp does not prove failure or success after a newer ACK. Final deployment checks must compare the live APIs with the read-only database, source hashes, preserved configuration/publication preferences, radio readiness and web health. No RF test messages or manual clock operations are sent.

The legacy log records confirmation time, not initial transmission time; verification conservatively requires a newer measurement than that confirmation. The pre-existing naive local advert schema cannot disambiguate repeated wall-clock times during an autumn DST transition.
