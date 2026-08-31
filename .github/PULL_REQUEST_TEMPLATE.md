<!--
Thanks for contributing. See CONTRIBUTING.md for setup and house rules.
PRs target `dev`, not `main`.
-->

## What this changes

<!-- A short description, and the issue it closes if there is one (e.g. "Closes #123"). -->

## Why

<!-- The problem being solved. For anything larger than a bug fix, link the issue where the approach was agreed. -->

## Testing

<!--
How you verified this. If it touches radio behavior, name the hardware you
tested against — board, transport, and firmware version. Much of this code
cannot be exercised without a device.
-->

## Checklist

- [ ] Branched from `dev` and targeting `dev`
- [ ] `make test` passes
- [ ] `make lint` passes (ruff + mypy)
- [ ] Frontend lint passes if templates changed (`npm run lint:frontend`)
- [ ] Tests added or updated for behavior changes
- [ ] `CHANGELOG.md` updated under `## [Unreleased]` if user-visible
- [ ] Config changes are reflected in `config.ini.example` (and the minimal/quickstart
      templates where relevant) — CI validates these with `validate_config.py --strict`
- [ ] New docs pages are added to `nav:` in `mkdocs.yml`
- [ ] Any new command justifies its airtime and defaults conservatively
