# Kebivo → Linear

[`relay.py`](relay.py) creates one Linear issue per delivered report with the
GraphQL `issueCreate` mutation. Linear reports GraphQL errors with HTTP 200, so
the relay checks `issueCreate.success` and answers Kebivo `424` when it is false.

## Dependencies

Python 3.10+ standard library, plus [`../relay_common.py`](../relay_common.py)
next to the `linear/` folder.

## Run

Create a personal API key under **Settings → Security & access → API**, and
find the team ID (for example with `query { teams { nodes { id name } } }` in
Linear's API explorer). Then:

```bash
WEBHOOK_SECRET=whsec_... \
LINEAR_API_KEY=lin_api_... \
LINEAR_TEAM_ID=<team UUID> \
LINEAR_LABEL_IDS=<label UUID>,<label UUID> \
KEBIVO_URL=https://feedback.example.org \
  python3 examples/recipes/linear/relay.py --host 0.0.0.0 --port 9100
```

`LINEAR_LABEL_IDS` and `KEBIVO_URL` are optional. A personal API key goes in the
`Authorization` header without `Bearer`; for an OAuth access token, change the
header to `Bearer <token>` in `relay.py`. Then follow the
[set-up steps](../README.md#set-up).

Each issue has the question as its title and a Markdown description with the
agent context, any proposed correction (marked unverified), project, times
asked, tags, Kebivo event ID and inbox link. A report that is asked again is
grouped in Kebivo, but each delivery creates a new Linear issue; route only
the reports you want as issues.

## Tested

On 2026-10-21, with a local Kebivo webhook destination (signing on) and a local
stand-in for `api.linear.app/graphql` (`LINEAR_API_URL`) that answers a
successful `issueCreate`: a knowledge gap was relayed with the expected
mutation, variables and `Authorization` header, and Kebivo got `204`.
**Untested:** the real Linear API (no Linear account used in this run).
