# Kebivo → Jira

[`relay.py`](relay.py) creates one Jira issue per delivered report through the
REST API v2 create-issue endpoint (`POST /rest/api/2/issue`), which takes a
plain-text (wiki markup) description on Jira Cloud and Jira Data Center.

## Dependencies

Python 3.10+ standard library, plus [`../relay_common.py`](../relay_common.py)
next to the `jira/` folder.

## Run

Jira Cloud, with an [API token](https://id.atlassian.com/manage-profile/security/api-tokens)
for an account that can create issues in the project:

```bash
WEBHOOK_SECRET=whsec_... \
JIRA_BASE_URL=https://your-site.atlassian.net \
JIRA_EMAIL=you@example.org JIRA_API_TOKEN=... \
JIRA_PROJECT_KEY=DOCS \
KEBIVO_URL=https://feedback.example.org \
  python3 examples/recipes/jira/relay.py --host 0.0.0.0 --port 9100
```

Jira Data Center with a personal access token: set `JIRA_BEARER_TOKEN` instead
of `JIRA_EMAIL` and `JIRA_API_TOKEN`.

Optional: `JIRA_ISSUE_TYPE` (default `Task`; it must exist in the project),
`JIRA_LABELS` (comma-separated, default `kebivo`; spaces become `-`) and
`KEBIVO_URL` for an inbox link. Then follow the [set-up steps](../README.md#set-up).

A required custom field or a missing issue type makes Jira answer `400`; the
relay logs Jira's error to stderr and answers Kebivo `424`. Add the field to
the `fields` object in `relay.py`, then retry the delivery in Kebivo.

## Tested

On 2026-10-21, with a local Kebivo webhook destination (signing on) and a local
stand-in for the Jira REST API that answers `201`: a knowledge gap was relayed
with the expected fields and a Basic `Authorization` header, and Kebivo got
`204`. **Untested:** a real Jira Cloud or Data Center site (no Atlassian
account used in this run).
