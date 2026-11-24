# Baliz: readable Time layout on phones

Base: `bd2cc25c0761016784d435a6046beadd2a65c689`.
Scope: local branch and LXC103 only; no GitHub publication.

Below 992px, the existing single target table becomes a card per relay, with a full-width name/measurement, labelled clock/battery/auto-clkreboot/delivery fields, and actions next to the title. The same DOM controls and handlers are used on desktop and mobile. Compact drift badges and the independent clock status are retained. The title and action header wrap, long keys/names wrap, and configuration/instructions are collapsed in an accessible native details element. Wider screens retain the table, contained within its section if needed.

Validation: six existing Time rendering/wiring checks passed. Jinja parsing, HTMLHint, ESLint (existing warnings only) and diff whitespace passed. Live visual checks cover phone widths, document overflow, access to all relay information/actions, and the desktop table. Deployment verifies source/configuration/preferences, radio and page health. No radio command or production setting change is issued by validation.

User preference follow-up: the Configuration panel remains always expanded as before; remove the native details/summary wrapper. Mobile relay cards and wrapping/overflow fixes remain.
