# DM delivery and clock synchronization

## Recipient preparation

DM recipients are resolved to one full public key. Ambiguous names or prefixes are rejected. A key-specific radio read verifies that the contact is present; a shared contact-list refresh handles cache misses.

If the contact is absent, complete pending-contact or stored advertisement data may restore it. The restored identity is read back before sending. An incoming advertisement path is not copied into the outbound route. Restoration does not evict another contact and respects `auto_manage_contacts = false`.

Concurrent sends to the same recipient are serialized. Contact cleanup skips recipients in use. Delivery reporting distinguishes a message that was not sent, an unconfirmed delivery, an ACK-confirmed delivery and a partially sent multipart reply. The existing boolean sending APIs remain available.

## ACK and clock replies

`[Bot] dm_min_ack_timeout` defaults to **30 seconds per attempt**. An explicitly configured value still applies, and an ACK ends the wait immediately. The retry count is unchanged.

Clock synchronization subscribes to replies before preparing or sending the DM. Replies received while waiting for an ACK are retained. A matching reply may confirm delivery when an ACK is missing; firmware errors remain recorded as errors. The reply's timestamp is not used to reject it because the target clock may be incorrect. The subscription is removed on completion or cancellation.

Replies have no request identifier. Association relies on the uniquely resolved target and the active listening window. An ACK or reply confirms delivery, not that the remote clock was corrected.

Scheduler timeouts cover contact preparation, ACK retries, reply collection and an optional clkreboot for each target. Existing per-target clkreboot opt-in and cooldown settings remain in effect.

## Clock measurements and display

The scheduler, Contacts, Time and map use the same latest timestamped observation. Message and sync-log receipt timestamps are UTC; advertisement receipt timestamps use the configured local timezone. Old observations and unset clocks remain distinguishable from recent measurements.

Time displays measured drift separately from the last delivery result and clock verification status. Contacts and Time share compact drift badges and observation dates. The Auto clkreboot switch saves its value and reloads the persisted setting if an update fails.

The map marks clocks exceeding `dashboard_max_clock_drift_seconds` with a clock symbol and includes the measured difference, source and observation date in the node details. Existing map filters and basemaps are retained. Time targets become cards on small screens; the configuration panel stays visible.
