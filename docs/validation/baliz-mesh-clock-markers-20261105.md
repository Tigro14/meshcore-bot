# Baliz: distinct clock markers and repeater clock details on the mesh map

Base: `f9a135e81c51f1b0a69f05f9b1eba0e58d7dc220`.
Scope: local branch and LXC103 only; no GitHub publication.

Mesh nodes receive the latest clock evidence from the same source-aware calculation as Time and Contacts. New metadata includes absolute/signed drift, receipt date, message/advert source, existing dashboard alert threshold, exceeded flag and stale-observation flag. No message content is included. The existing node timeframe, location and role filters remain authoritative.

A small clock glyph is distinct from the existing region/one-byte triangle. Both indicators can coexist. Clock-only evidence does not become a region/one-byte alert or change the focus-mode classification/colors. Labels remain permanent and clickable with names hidden; flagged nodes use the existing foreground pane. Map popups and graph tooltips contain a separate clock note with measured advance/delay, date/source and an explicit stale-evidence label when needed. An ACK alone cannot create or remove a drift marker.

The existing `dashboard_max_clock_drift_seconds` setting controls the clock marker (300 seconds by default). An old measured drift can remain marked on a node retained by the map filters; the date and stale label prevent presenting it as a fresh measurement. A newer good clock sample clears the marker. Existing configuration warning logic and synchronization/reboot policy are unchanged.

Validation: 141 targeted Python 3.13 checks passed, two skipped, and the same two unrelated legacy web checks deselected. Twenty-two clock/API checks passed on Python 3.10. Twenty-four JavaScript checks passed across map labels, Clock and Contacts, including distinct icons, coexistence, keyboard/click activation, numeric drift, signed direction, stale dates and preservation of region/one-byte triangles. Python lint/compilation, diff whitespace, log-injection scan, HTMLHint and ESLint passed (existing ESLint warnings only).

Deployment verifies source hashes, unchanged configuration/channel visibility/clock preferences, live node metadata, service/radio health and the actual map UI. No production clock setting is changed and no RF test message is sent.

Follow-up appearance: replace the filled/dark clock with a thin blue-grey outline SVG on a transparent background, with a lighter night variant. The clock remains separate from the yellow triangle. Node popups now pan into view and have a bounded content height so the added clock detail and close button remain accessible. Six map-rendering checks, HTMLHint and ESLint passed for this adjustment.

Final appearance requested by the user: white round clock face with dark-grey outline and hands, including on the night map. The thin transparent version was too faint. No clock/threshold logic changed.

Popup/priority follow-up: wider responsive node cards, wrapping key/table cells, horizontal overflow prevention and a larger viewport-bounded vertical allowance accommodate combined configuration/clock notes. Configuration warning tooltips use a dedicated z-index 680 pane, above clock labels (670) and ordinary labels (650), while popups remain above them (700). Interactive labels retain their click/keyboard behavior. Seven map-label/priority checks plus HTMLHint/ESLint passed; final layout is inspected on the live map.
