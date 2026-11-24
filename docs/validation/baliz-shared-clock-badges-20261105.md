# Baliz: use Contacts drift badges on Time

Base: `f78946664086498d8b10add701c0ef86bfe613e5`.
Scope: local branch and LXC103 only; no GitHub publication.

Contacts' existing compact drift formatting, colors and clock glyph are shared through `static/js/clock_drift.js`. Both pages load the same renderer. Time's measured-drift column uses the observation status returned by the API, independent of the transmission/verification state in its Horloge column. Dates/sources and auto-clkreboot persistence remain unchanged. Unknown measurements use N/D, stale samples remain grey, and large drift displays as days/hours rather than raw seconds.

Validation: 18 JavaScript rendering checks passed across Time and Contacts, including Concarneau's compact numeric badge alongside separate verification. Sixteen clock/API checks passed, including matching observation status in the two APIs. Python lint/compilation, HTMLHint, ESLint and diff whitespace passed (existing ESLint warnings only). Final production checks verify the shared asset, served pages, source/configuration/preference hashes, radio readiness and the actual Time rendering. No radio test transmission or clock-setting change is performed.
