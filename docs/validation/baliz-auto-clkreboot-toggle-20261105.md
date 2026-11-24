# Baliz: connect the Auto clkreboot switch to persistence

Base: `d47533c4567755a1bd64f788e27ba1d25e2fd885`.
Scope: local branch and LXC103 only; no GitHub publication.

The checkbox's inline handler called `#TimeViewer.toggleAutoClkreboot`, but DOM initialization bound only toggleTarget, promptDelete and showHistory. The native checkbox changed visually without sending a request, then the 15-second refresh restored the stored false value. No scheduler logic clears this opt-in automatically.

Bind the existing method during initialization. Check HTTP success before treating the update as saved; report a failure explicitly and reload the stored value in all cases. The server's existing update/persistence logic, clock-ahead error condition, per-target opt-in and reboot cooldown remain unchanged. Do not enable any production target on the user's behalf.

Validation: six JavaScript checks passed, including the actual DOMContentLoaded wiring, resulting PUT, and error handling. Twenty-one Python checks passed, including opt-in/out persistence across repeated reads and the existing auto-clkreboot logic. Lint, HTMLHint and ESLint passed (existing warnings only). Production verification is read-only: served template wiring, API values, unchanged target preferences, source hashes and service/radio health; no radio command or production settings update is issued by validation.
