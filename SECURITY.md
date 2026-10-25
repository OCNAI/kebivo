# Security policy

## Supported versions

Kebivo has not published a release yet. The first version will be 0.1.0. Until 1.0, only the latest release receives security fixes; upgrade to it before you report a problem that might already be fixed.

| Version | Supported |
| --- | --- |
| Latest 0.x release | Yes |
| Older 0.x releases | No |
| Unreleased `main` | Best effort |

## Reporting a vulnerability

**Do not open a public issue, discussion or pull request for a security problem.**

Report vulnerabilities privately through GitHub private vulnerability reporting: open the [**Security** tab](https://github.com/OCNAI/kebivo/security) of `OCNAI/kebivo` and choose **Report a vulnerability**.

Please include:

- the Kebivo version (Settings page or `python -m kebivo.cli version`) or the commit;
- how Kebivo is deployed (Compose, reverse proxy, relevant settings, with secrets removed);
- the affected component, for example authentication, project tokens, MCP, webhooks, SMTP, LLM providers, export or retention;
- steps to reproduce, and a proof of concept if you have one;
- the impact you expect, and whether you know of active exploitation.

Never send real credentials, tokens, keys or personal data. Use synthetic values.

## What to expect

These are targets, not guarantees.

- An acknowledgement within 7 days.
- An assessment and, when the report is confirmed, a planned fix within 30 days of the acknowledgement; we tell you if it takes longer.
- A fix in the next release with a `CHANGELOG.md` entry. You are credited unless you ask not to be.
- Please give us reasonable time to release a fix before you disclose the problem publicly. We will agree a disclosure date with you.

A small team maintains Kebivo. There is no paid bug bounty.

## Scope

In scope: the Kebivo application, the container image built from this repository, the default Compose configuration and the examples.

Out of scope: vulnerabilities in your reverse proxy, Docker host, LLM provider or receiving systems; reports that require a stolen admin password, `SECRET_KEY` or `ENCRYPTION_KEY`; missing hardening that the [self-hosting guide](docs/self-hosting.md) tells operators to configure (TLS, loopback binding, `TRUSTED_PROXY_IPS`); denial of service by volume alone; and findings from automated scanners without a demonstrated impact.

## Security design in brief

- One local administrator, Argon2-hashed password, digest-only session tokens, idle timeout, login throttling, Origin and CSRF checks on every cookie-authenticated write.
- Project tokens are scoped (`feedback:gap`, `feedback:correction`), stored as keyed digests, shown once, can expire and can be revoked. They never authorize admin APIs.
- Destination secrets and LLM provider keys are encrypted with AES-256-GCM using `ENCRYPTION_KEY`, are write-only in the API, and never appear in logs.
- Webhooks and LLM provider calls block private, loopback and metadata addresses unless the destination or provider opts into private networks; metadata and link-local addresses are always blocked, also for SMTP. DNS is checked for each attempt and webhook redirects are not followed.
- Logs contain request metadata and error categories, never feedback content or credentials.
- Feedback content is **not** encrypted at rest. Protect the data volume and backups as described in the [self-hosting guide](docs/self-hosting.md#secrets-and-what-to-protect).
