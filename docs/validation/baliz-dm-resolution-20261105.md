# Baliz DM contact preparation — local production change, 2026-11-05

Base: `72315457c6f97136a78b8cb29b5bbbcdbdc222bc`.
Publication: local branch and LXC103 only; do not push to GitHub without the user's later instruction.

## Behavior

All ordinary DM replies, long replies and clock-sync targets use a common unique identity resolver. Names and prefixes must identify one full 32-byte key across the live cache, pending contacts and historical records. A full key is authoritative. Clock sync uses the same selection rather than taking the first matching record.

Contacts are verified by a key-specific local radio read. A very short verified cache prevents duplicate reads. A miss can cause one shared complete contact refresh. The payload of the complete refresh is authoritative; the library's merged historical cache is not used as proof of existence.

If a contact is still absent, complete native contact data or a previously captured signed announcement may restore that exact node on demand. Existing verified paths/flags are retained. Historical announcement paths are never copied into an outbound route: restored contacts start with no direct route, allowing the firmware's existing flood discovery. A successful update must be followed by a matching key-specific read before a DM is attempted.

Preparation is bounded. Same-recipient preparation/sending is serialized, complete refreshes are coalesced, and restorations are serialized with capacity checks. No cleanup is triggered to make room. A full table, unknown capacity, incomplete data or disabled automatic contact management produces an explicit non-emission result. Active recipients are protected from bot cleanups; successful removal invalidates the matching cached identity.

Per-request delivery metadata distinguishes not emitted, delivery unconfirmed, ACK-confirmed and partially sent. Existing bool-returning sending APIs are retained. Clock sync no longer waits for a possible reply when no frame could be emitted. An internal retry-library AttributeError does not cause a second uncontrolled fallback send. Existing ACK wait/retry settings, assistant intent routing, configuration, privacy preferences and RF statistics are unchanged.

## Validation

- 723 targeted Python tests passed: contact preparation, delivery reporting, clock/battery scheduler, command manager, cleanup, web capture, channel privacy, log redaction and mentions.
- 34 new contact/delivery checks passed on Python 3.10; the same checks pass on Python 3.13. An additional final selection covering the unchanged catalogue, city grounding and compatibility passed 69 tests.
- Five JavaScript rendering tests passed for truthful delivery labels and legacy failures.
- Python lint, compilation, log-injection check (205 files), HTMLHint and ESLint (warnings only, no errors) passed.
- Pure, read-only validation against the two retained production announcements confirmed that both contain sufficient restoration data. No contact was added and no RF test message was transmitted by that check.

Production deployment must verify unchanged configuration/publication digests, fresh radio channel cache, source hashes and healthy web pages. The first actual restoration is triggered only by a legitimate requested DM or an already-configured scheduled operation.
