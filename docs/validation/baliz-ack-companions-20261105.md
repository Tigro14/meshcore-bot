# Baliz: ACK waits, early clock replies and companion-only assistant

Base: `d6e42065063a0c60bb2bf0b2d457aa6738af1110`.
Scope: local branch and LXC103 only. Do not publish to GitHub.

## Behavior

- Only a uniquely identified origin whose advertised device type is Companion (native type 1) may invoke the assistant, direct LLM command or LLM-backed mesh service. The service boundaries also enforce the rule for conversation, Wiki and tool reformulation. Repeaters, room servers, sensors, unknown types and ambiguous origins are denied silently, on channels and in DMs. No role inference from names, message words or RF-path repeaters is used. Historical `role` guesses are ignored; the original advertisement's explicit type/mode can identify companions absent from the current radio cache.
- Receive-path prefix expansion now requires a unique full identity. No radio contact read/write or RF transmission is triggered by the assistant policy. Message recording, RF observations and other non-LLM commands retain their existing processing.
- The default ACK wait floor is 30 seconds per existing attempt, instead of 8. An ACK still completes the attempt immediately. The attempt counts, flood policy and LLM timeouts are unchanged. LXC103 has no explicit ACK-floor option, so its configuration file remains unchanged.
- Clock sync subscribes before DM recipient preparation/sending. Replies received during ACK retries are retained and can confirm delivery even if the ACK is missing. The existing extra reply window still applies when needed. Receipt during the active subscription establishes freshness; a remote timestamp is not trusted when the operation's purpose is to repair that clock.
- Firmware errors remain errors in the clock-attempt details even when delivery is confirmed. They do not become LLM requests. Auto-clkreboot still requires the existing per-target opt-in and cooldown. Subscriptions are removed on success, failure and cancellation.
- Scheduler wrapper deadlines account for recipient preparation, retries, reply windows and optional clkreboot across the configured targets. This avoids the former fixed 300-second budget becoming too short after increasing the ACK floor.

## Validation

- 1,119 targeted Python 3.13 tests passed: companion eligibility and actual command-manager dispatch, early/late clock replies with the real event dispatcher, ambiguous identities, cancellation, contact restoration, ACK delivery, scheduler, receive handling, privacy, channel visibility, web capture and closed-catalog routing.
- 93 focused checks passed on Python 3.10, including the timeout/no-reply paths.
- Python lint, compilation, diff whitespace and log-injection checks passed (207 runtime files scanned).
- The legacy assistant integration selection has the same 25 failures on the unchanged base and the updated version; no additional failure appeared. These fixtures still expect the older router behavior. They are not counted among the passing checks above.
- No live RF test messages, clock commands, contact modifications or model requests were sent by validation. Production checks use source hashes, read-only metadata, advertised-type eligibility and service/page health.

Delivery confirmation is separate from successfully setting a remote clock. Clock firmware replies have no request nonce; association remains best effort within the active target subscription. Channel sender names and advertised types are metadata, not a new authentication mechanism.
