# Baliz: keep measured drift visible independently of verification

Base: `4fd0dac37dffd547b3315b30eb57d519976cff40`.
Scope: local branch and LXC103 only; no GitHub publication.

The numeric difference remains visible even when the sender's timestamp denotes an unset clock. Time's **Décalage mesuré** column always shows that measured difference when a sample exists. Its **Horloge** column retains the separate verification state, including **Horloge à vérifier** after ACK without newer evidence.

Contacts always displays the measured drift, with its existing compact duration and observation date/source. Its badge is based on the observation rather than the latest synchronization attempt. Pending, rejected, unset and stale states no longer replace the duration with a status label; stale evidence can still use a grey badge and descriptive tooltip. Missing measurements remain N/D. Full verification metadata remains available separately in the API.

Validation: 353 targeted Python 3.13 tests passed, two skipped and the same two previously demonstrated unrelated legacy web tests deselected. Fifteen clock checks passed on Python 3.10. Sixteen JavaScript checks passed, including Concarneau's large measured drift and ensuring verification text is confined to Time's clock-status column. Python lint/compilation, diff whitespace, HTMLHint and ESLint passed (existing ESLint warnings only).

No changes to measurement calculations, synchronization/retry policy, configuration, database rows or channel privacy preferences. No RF test transmissions are made. Deployment verifies source hashes, radio/page health, and matching numeric drift in both live APIs.
