# Kebivo → Microsoft Teams

[`relay.py`](relay.py) posts each signed delivery as an Adaptive Card to a
Microsoft Teams **Workflows** incoming webhook. The relay uses Python 3.10+
standard library and [`../relay_common.py`](../relay_common.py).

## Run

In Teams, create a Workflows webhook that posts an Adaptive Card to your chosen
channel or chat, and copy its URL. Then run:

```bash
WEBHOOK_SECRET=whsec_... \
TEAMS_WEBHOOK_URL=https://... \
KEBIVO_URL=https://feedback.example.org \
  python3 examples/recipes/teams/relay.py --host 0.0.0.0 --port 9100
```

`WEBHOOK_SECRET` is the Kebivo webhook destination's signing secret;
`TEAMS_WEBHOOK_URL` is the Workflows webhook URL. `KEBIVO_URL` is optional and
adds an **Open in Kebivo** card action. Follow the shared
[setup steps](../README.md#set-up) to configure the Kebivo destination and rule.
Keep the Workflows URL private: anyone with it can post to that workflow.

The card includes the question, project, repeat count, tags, context and any
proposed correction (marked unverified). Test sends have `[Kebivo test]` in
the title. The relay remembers forwarded webhook IDs in memory; a restart can
forward a retried delivery twice.

## Tested

On 2026-10-29, a local signed webhook sender posted a test knowledge gap to the
relay with a local HTTP stub in place of Teams. The stub received the Adaptive
Card, including the question, context and inbox link, and the relay answered
`204`. Replaying the webhook ID did not post again. Bad signatures and JSON
answered `401` and `400`; stub responses `429` with `Retry-After: 12` and `503`
produced relay responses `429` with the retry delay and `502`. A real Teams
workflow was not used.
