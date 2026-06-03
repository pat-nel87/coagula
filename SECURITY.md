# Security Policy

## Supported versions

Only the latest minor release on `main` receives security fixes. Older
tagged versions are not maintained.

| Version | Supported |
|---------|:---------:|
| 0.3.x   | ✅        |
| < 0.3   | ❌        |

## Reporting a vulnerability

**Please do not file public GitHub issues for security problems.**

Report security issues privately via one of these channels:

- **Preferred:** [GitHub Security Advisories](https://github.com/pat-nel87/coagula/security/advisories/new)
  — encrypted, tracked, and gives you a CVE workflow if applicable.
- **Email:** open a private security advisory on GitHub (above) — no email
  inbox is currently monitored.

Please include:

- A description of the vulnerability and its impact
- Steps to reproduce (or a minimal proof of concept)
- Any suggested mitigations or patches
- Whether you'd like to be credited in the advisory

## What to expect

- Acknowledgement within 7 days (best-effort; this is a small project).
- Investigation and a fix (or a decision that no fix is needed) within
  30 days for confirmed issues.
- Coordinated disclosure once a fix is available.

## Out of scope

- Vulnerabilities in *transitive* dependencies — please report those
  upstream. We'll bump our pin once a fix is published.
- Issues in third-party services (Ollama, Azure OpenAI, MCP hosts) — those
  belong with their respective vendors.
- Self-inflicted misconfiguration (`--allow-all-tools` running attacker-
  controlled content, exposing API keys in shell history, etc.) — that's a
  documentation issue at most, not a vulnerability.
