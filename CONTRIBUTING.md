# Contributing to MeshCore Bot

Thanks for your interest. This project runs on real radios on real meshes, so
the bar for changes is partly technical and partly about being a good mesh
citizen — see [Airtime matters](#airtime-matters) below.

## Before you start

For anything larger than a bug fix, open an issue first. It is cheaper to agree
on an approach than to rework a finished pull request.

If your change is specific to your own deployment — a custom command, a private
service integration — it probably belongs in `local/` rather than upstream. See
[docs/local-plugins.md](docs/local-plugins.md).

## Development setup

Requires Python 3.10 or newer.

```bash
git clone https://github.com/agessaman/meshcore-bot
cd meshcore-bot
make dev          # creates .venv, installs runtime + test + lint dependencies
```

## Running the checks

CI runs six jobs. You can reproduce all of them locally, and doing so before
pushing saves a round trip:

```bash
make test                                   # pytest (CI matrix: 3.10–3.13)
make lint                                   # ruff check + mypy
python scripts/check_log_injection.py       # no new unsanitized logger calls
npm ci && npm run lint:frontend             # HTMLHint + ESLint on templates
shellcheck --severity=warning **/*.sh       # shell scripts
```

`make fix` auto-fixes most ruff findings.

Ruff is pinned to `0.15.15` in `pyproject.toml` (`required-version`). An
unpinned `pip install ruff` will disagree with CI — use `make dev`.

## House rules

### Airtime matters

Every command costs shared, unlicensed spectrum that the whole mesh depends on.
New commands should justify their airtime: prefer terse replies, respect
`max_response_hops`, and default anything chatty to off. Features that spend
airtime on a schedule need a conservative default interval.

### Database migrations are append-only

Add a new numbered migration; never edit or remove an existing one. See
**Database Migration** under
[Adding New Plugins](README.md#adding-new-plugins) in the README, which also
covers adding commands and service plugins.

### Config changes travel with their examples

CI validates every shipped config against the schema with
`validate_config.py --strict`. A new setting needs an entry in
`config.ini.example` and, where relevant, in the minimal and quickstart
templates.

### Docs changes travel with their nav entry

New pages under `docs/` must be added to `nav:` in `mkdocs.yml`, or they will
not appear on the documentation site.

### Logging is sanitized

User-controlled values must not flow unescaped into log calls.
`scripts/check_log_injection.py` enforces this against a baseline.

## Pull requests

- Branch from `dev` and target `dev` — not `main`.
- Commit messages follow Conventional Commits: `feat(scope):`, `fix(scope):`,
  `docs:`, `perf(scope):`, `build(scope):`.
- Add tests for behavior changes.
- Add a `CHANGELOG.md` entry for anything user-visible, under an
  `## [Unreleased]` heading at the top of the file — create that heading if the
  previous release has just been tagged and it is not there. The format follows
  [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
- Note any hardware you tested against — board, transport, and firmware version.
  Much of this code can only be exercised properly on a device.

## Reporting bugs

Open an issue including the bot version (`git describe --tags`), the relevant
`config.ini` section with keys and tokens redacted, and log output from around
the time of the failure.

## Security

Please do not report security issues in a public issue — see
[SECURITY.md](SECURITY.md) for private reporting.

## License

Contributions are accepted under the MIT License (see [LICENSE](LICENSE)).
