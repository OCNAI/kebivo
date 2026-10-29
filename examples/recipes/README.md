# Webhook recipes: Slack, Linear, Jira and Teams

Kebivo has no native Slack, Linear, Jira or Teams integration. A webhook destination
can reach them through a small relay you run yourself. Each relay is one Python
file plus [`relay_common.py`](relay_common.py). They use only the standard
library (Python 3.10+, no `pip install`).

| Recipe | Creates | Needs |
| --- | --- | --- |
| [`slack/`](slack/) | A message in a channel | A Slack incoming webhook URL |
| [`linear/`](linear/) | A Linear issue per delivery | A Linear API key and team ID |
| [`jira/`](jira/) | A Jira issue per delivery | A Jira site, credentials and project key |
| [`teams/`](teams/) | An Adaptive Card in a Teams channel or chat | A Teams Workflows webhook URL |

Each relay:

1. checks the Standard Webhooks signature, the same way as
   [`webhook_receiver.py`](../webhook_receiver.py), and rejects bad or stale ones;
2. skips a `webhook-id` it has already forwarded, because Kebivo delivers at
   least once (the memory is per process, so a restart can forward a retry twice);
3. maps the [payload](../../docs/webhook-payload.schema.json) to one request and
   answers Kebivo with a status that makes Kebivo's retries do the right thing:

| Relay answers | When | Kebivo |
| --- | --- | --- |
| `204` | Target accepted it, or the webhook-id was already forwarded | Marks the delivery delivered |
| `401` / `400` | Bad signature / body is not JSON | Permanent failure |
| `429` + `Retry-After` | Target rate-limits | Retries after the given delay |
| `502` | Target is down, times out or answers 5xx | Retries on the backoff schedule |
| `424` | Target answers another 4xx or reports an error (wrong key, project or team) | Permanent failure: fix the relay settings, then retry the delivery |

The relay logs one JSON line per delivery to stdout and the target's error to
stderr. It never logs the payload or credentials.

## Set up

1. Run the relay where Kebivo can reach it, for example as a sidecar on the same
   Compose network or on the host. The examples below listen on port 9100.
2. In the admin UI, add a **Webhook** destination pointing at the relay
   (`http://relay:9100/`), with signing on. Copy the signing secret
   (`whsec_...`) that Kebivo shows once.
   A relay on a private address needs **Allow private network** on the
   destination.
3. Add a routing rule that sends the reports you want to that destination.
4. Start the relay with `WEBHOOK_SECRET` set to the signing secret, plus the
   target settings from the recipe README.
5. Use **Test send** on the destination. Test deliveries are titled
   `[Kebivo test]`.

Set `KEBIVO_URL` (your Kebivo origin) on the relay to add a link to the inbox
group in each message.

Behind a public URL, put the relay behind your TLS proxy. Keep it off the
public internet when it does not need to be there: it only accepts signed
requests, but it holds the target credentials.

## No-code alternative

Automation tools (n8n, Zapier, Make) can receive the webhook and create the
message or issue with a mapping. Verify the signature there if the tool can
(Standard Webhooks, HMAC-SHA256 over `webhook-id.webhook-timestamp.body`), or
use a destination auth header (bearer or custom header) as a shared secret.
Map these payload fields:

| Payload field | Use it for |
| --- | --- |
| `event_type` | `knowledge_gap` or `knowledge_correction` |
| `feedback.question` | Title or message text |
| `feedback.context`, `feedback.correction` | Description (the correction is unverified) |
| `project.name`, `tags`, `occurrence_count` | Labels, fields or text |
| `group_id` | Link: `<KEBIVO_URL>/inbox/groups/<group_id>` |
| `test` | `true` for **Test send** deliveries |

These mappings were not run in a no-code tool.
