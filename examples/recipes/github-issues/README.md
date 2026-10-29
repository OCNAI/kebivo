# Kebivo → GitHub Issues

[`relay.py`](relay.py) creates one issue per signed Kebivo delivery with the
GitHub REST API. It uses Python 3.10+ standard library and
[`../relay_common.py`](../relay_common.py).

## Run

Create a fine-grained GitHub token with **Issues: write** permission for the
target repository. Create any desired labels in that repository first; GitHub
can silently omit labels when the token owner lacks push access. Then run:

```bash
WEBHOOK_SECRET=whsec_... \
GITHUB_TOKEN=<fine-grained-token> \
GITHUB_REPOSITORY=owner/repo \
KEBIVO_URL=https://feedback.example.org \
  python3 examples/recipes/github-issues/relay.py --host 0.0.0.0 --port 9100
```

`WEBHOOK_SECRET` is the Kebivo destination's signing secret.
`GITHUB_REPOSITORY` names the target repository; `KEBIVO_URL` is optional and
adds an inbox link to the issue body. For a GitHub Enterprise server, set
`GITHUB_API_URL` to its REST API root. Follow the shared
[setup steps](../README.md#set-up) to configure the Kebivo destination and rule.
Keep the token private and route only the reports you want to create as issues.

The question is in the truncated title and full body. The body also includes
context, any proposed correction (marked unverified), project, repeat count,
tags and event ID. Kebivo tags are sent as issue labels. A successful delivery
returns `204`; GitHub rate limits return `429` with `Retry-After`, and GitHub
`5xx` returns `502` so Kebivo retries. Other `4xx` errors return `424`.
The relay remembers forwarded webhook IDs in memory; a restart can forward a
retried delivery twice.

## Tested

On 2026-10-29, a local signed webhook sender posted a test knowledge gap to the
relay with a local HTTP stub in place of GitHub. The stub received
`POST /repos/owner/repo/issues` with the bearer token, question, body and tag
label, and the relay answered `204`. Replaying the webhook ID did not create a
second issue. Bad signatures and JSON answered `401` and `400`; stub responses
`429` with `Retry-After: 12`, `503`, and rate-limited `403` produced `429`,
`502`, and `429` with a retry delay. A plain `403` produced `424`. A real
GitHub repository was not used.
