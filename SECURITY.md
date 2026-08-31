# Security Policy

## Supported versions

| Version | Supported |
|---------|-----------|
| 1.0.x   | ✅ Security fixes |
| < 1.0   | ❌ Please upgrade |

## Reporting a vulnerability

Please report security issues privately rather than opening a public issue.

Use GitHub's private vulnerability reporting: go to the
[**Security** tab](https://github.com/agessaman/meshcore-bot/security) of this
repository and choose **Report a vulnerability**. The report is visible only to
the maintainers — nothing is disclosed publicly until a fix is ready.

Please include:

- A description of the issue and the impact you believe it has
- The affected component and version (`git describe --tags`)
- Steps to reproduce
- Any relevant configuration, with keys and tokens redacted

**What to expect:** acknowledgement within 10 days and an assessment within 30
days, followed by coordinated disclosure once a fix is available. This is a
volunteer-maintained project — there is no bug bounty, but reporters are
credited in the changelog unless they would rather not be.

## Scope

In scope:

- **Web viewer** — authentication bypass, session handling, XSS, CSRF, CSP
  bypass, or any unauthenticated path to the radio-control endpoints.
- **Outbound HTTP** — SSRF in feed fetching, webhooks, URL shortening, or
  weather and geocoding providers.
- **Inbound webhook service** — authentication and input handling.
- **Credential handling** — leakage of API keys, MQTT credentials, or bridge
  webhook URLs into logs, HTTP responses, or MQTT payloads.
- **Command authorization** — bypass of `[Admin_ACL]`, ban lists, or
  `[Rate_Limits]`.
- **Log injection** — user-controlled input forging or corrupting log records.

Out of scope:

- **The MeshCore protocol and firmware itself.** Report those upstream to the
  [MeshCore project](https://github.com/meshcore-dev/MeshCore).
- **RF-layer attacks** — jamming, flooding, or spoofing on an open, unlicensed
  mesh where messages are unauthenticated by design.
- **Running the web viewer without a password on an untrusted network.** The
  viewer's password is optional; when it is unset the interface is open to
  anyone who can reach the port. That is documented behavior, not a
  vulnerability — see [docs/web-viewer.md](docs/web-viewer.md).
- **Denial of service** achieved by exhausting shared mesh airtime.

## Deployment guidance

The web viewer is designed for a trusted LAN. If you expose it more widely, set
a password, terminate TLS at a reverse proxy, and restrict access at the network
layer.

Configuration files hold API keys and broker credentials. The service installers
create a dedicated service account and set `0700`/`0750` modes on the
configuration, state, and log directories for that reason — preserve those
permissions if you install by hand.
