# Kebivo → Slack

[`relay.py`](relay.py) posts each delivered report to a Slack channel through an
[incoming webhook](https://api.slack.com/messaging/webhooks). It escapes `&`,
`<` and `>` so user text cannot create Slack links or mentions.

## Dependencies

Python 3.10+ standard library, plus [`../relay_common.py`](../relay_common.py)
next to the `slack/` folder.

## Run

Create a Slack app with an incoming webhook for the channel, then:

```bash
WEBHOOK_SECRET=whsec_... \
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/T000/B000/XXXX \
KEBIVO_URL=https://feedback.example.org \
  python3 examples/recipes/slack/relay.py --host 0.0.0.0 --port 9100
```

`KEBIVO_URL` is optional and adds an **Open in Kebivo** link. Then follow the
[set-up steps](../README.md#set-up).

A message looks like:

```
*Knowledge gap: Where is the travel policy?*
Project: Support bot
Asked 3 times
Tags: hr
Context: searched the HR wiki
<https://feedback.example.org/inbox/groups/…|Open in Kebivo>
```

## Tested

On 2026-10-21, with a local Kebivo webhook destination (signing on) and a
local stand-in for `hooks.slack.com` that answers like Slack (`200 ok`):

- a knowledge gap routed by tag was relayed, Slack got the escaped message
  with the inbox link, and Kebivo got `204`;
- replaying the same `webhook-id` answered `204` without a second post; a bad
  signature got `401`, a non-JSON body `400`, and a target answering `503`
  made the relay answer `502`.
- **Untested:** a real Slack workspace (no Slack account used in this run).
