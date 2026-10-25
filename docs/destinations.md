---
title: Webhook and email destinations
description: Configure Kebivo webhook and SMTP destinations, verify signed payloads, test delivery and inspect retries for routed feedback.
---

# Destinations

## Using the dashboard

Open **Destinations** at `/destinations`. The list shows each destination's type, enabled state and delivery mode. For webhooks, it shows the origin but hides the path. Create or edit webhook and SMTP settings here. Saved secrets are masked; you can replace or clear them, but the API will not return their values. Copy a signing secret before dismissing the panel because it appears only once after creation or rotation. **Test** works even when a destination is disabled. The built-in inbox can only be enabled or disabled.

Admin cookie sessions manage destinations through `GET/POST /api/v1/destinations` and `GET/PATCH/DELETE /api/v1/destinations/{id}`. Writes require `Origin` and `X-CSRF-Token`. Names are unique without regard to case. The built-in inbox is listed and can only be enabled or disabled. The inbox keeps every accepted event regardless of routing.

A destination has a `name`, `type` (`webhook` or `smtp`), `enabled`, `delivery_mode` (`new_groups_only` or `every_occurrence`) and a type-specific `config` object. `new_groups_only` sends new and reopened groups, or the first non-failed delivery to that destination for a group; `every_occurrence` sends each routed event. See [deduplication](deduplication.md). Webhook and SMTP destinations also have `allow_private_network`, which defaults to false. Opt in only for a trusted private endpoint; the sender checks resolved addresses and pins the selected address for the connection.

`POST /api/v1/destinations/{id}/test` sends a synthetic `test:true` message and records a `destination.tested` audit event with outcome and category, even when the destination is disabled. It creates no delivery or attempt row and is limited to ten calls per minute per admin session. Webhook sends use `X-Delivery-ID`; signing uses the Standard Webhooks headers described below. See [routing and delivery](routing.md) for retries and logs.

Webhook configuration: `url`, `auth` (`{"type":"none"}`, bearer with `token`, or header with `name` and `value`), optional `signing`, up to 10 `custom_headers`, and `timeout_seconds` from 1 to 30 (default 10). URLs require HTTPS unless private network access is enabled; userinfo and fragments are rejected. Literal metadata, link-local, and other always-blocked IP addresses are rejected when saving, even with `allow_private_network=true`. Trusted private addresses such as `10.0.0.1` remain available with the opt-in. Header names must be valid HTTP tokens. Hop-by-hop, Host, Content-Length, Content-Type, Authorization and `webhook-*` names are reserved.

SMTP configuration: `host`, `port`, `tls_mode` (`starttls` by default, `implicit_tls`, or `none`), optional `username` and `password`, `from`, 1 to 20 `to` addresses, and `timeout_seconds` from 1 to 30 (default 15). A password requires TLS.

The webhook URL, bearer token, header values, signing secret and SMTP password are encrypted in the destination row. Responses show only `{ "set": true }` for a stored secret. The webhook response also gives `url_origin` and `url_has_path`, never the path itself. On PATCH, an omitted field keeps its value; `null` clears it and a string replaces it, subject to validation. A signing secret is returned once when signing is enabled or from `POST /api/v1/destinations/{id}/rotate-signing-secret`; save it immediately. A destination referenced by any rule, fallback or delivery cannot be deleted.

Set `ENCRYPTION_KEY` to 64 random hex characters (`openssl rand -hex 32`) in production and back it up with SQLite. Destination secret JSON uses AES-256-GCM, bound to the destination ID. To rotate the key, set a new `ENCRYPTION_KEY`, put the old key in `ENCRYPTION_KEY_PREVIOUS`, stop the application, run `python -m kebivo.cli reencrypt-secrets`, then remove the old key. Startup refuses a destination whose key is unavailable.

For `make dev`, export a stable `ENCRYPTION_KEY` before starting. Without one, each reload generates a fresh key and previously saved destination secrets become unreadable. A damaged secret row is still listed with `secrets_unavailable: true`; metadata can be edited, while secret or config edits return `409` until the secret is repaired.

## Webhook payload and verification

Webhook requests carry JSON conforming to [payload schema v1.0](webhook-payload.schema.json). A delivery contains the following fields; optional feedback values are JSON `null`:

```json
{"schema_version":"1.0","delivery_id":"8db7199a-8e08-4ac5-b67d-99a75ccfe3dd","event_id":"73082266-8ea0-4be6-9842-fca43c099730","group_id":"756bf342-124d-4bd2-af29-1ec986d49681","event_type":"knowledge_gap","project":{"id":"35cad774-97bf-4dca-9e30-fcf44d407f06","name":"Example"},"feedback":{"question":"How does this work?","correction":null,"context":null},"tags":["docs"],"occurrence_count":1,"created_at":"2026-10-17T12:00:00+00:00","test":false}
```

The payload excludes metadata, user references, token data and IP addresses. A test send has `test:true` and synthetic IDs. The sender sets `Content-Type: application/json`, `X-Delivery-ID`, and `User-Agent: Kebivo-webhook/1`. Authentication can be absent, a bearer token in `Authorization`, or one configured static header. Up to ten other custom headers are supported.

When signing is enabled, `webhook-id` is the delivery ID and stays fixed across retries. `webhook-timestamp` is Unix seconds. `webhook-signature` is `v1,` followed by base64 of HMAC-SHA256 over `webhook-id + "." + webhook-timestamp + "." + raw_body`, using the base64-decoded part of the `whsec_` secret as key. Verify the raw request bytes before parsing JSON, compare signatures in constant time, and reject timestamps more than five minutes from your clock. Deduplicate accepted requests by `webhook-id` because retries can repeat a delivery.

Python reference:

```python
import base64, hashlib, hmac, time

def verify(headers, body, secret):
    stamp = headers["webhook-timestamp"]
    if abs(time.time() - int(stamp)) > 300:
        return False
    key = base64.b64decode(secret.removeprefix("whsec_"), validate=True)
    signed = f'{headers["webhook-id"]}.{stamp}.'.encode() + body
    expected = "v1," + base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
    return hmac.compare_digest(expected, headers["webhook-signature"])
```

Node.js reference (raw request body as a `Buffer`):

```js
import { createHmac, timingSafeEqual } from 'node:crypto';

function verify(headers, body, secret) {
  const stamp = headers['webhook-timestamp'];
  if (Math.abs(Date.now() / 1000 - Number(stamp)) > 300) return false;
  const key = Buffer.from(secret.replace(/^whsec_/, ''), 'base64');
  const signed = Buffer.concat([Buffer.from(`${headers['webhook-id']}.${stamp}.`), body]);
  const expected = Buffer.from('v1,' + createHmac('sha256', key).update(signed).digest('base64'));
  const actual = Buffer.from(headers['webhook-signature'] || '');
  return actual.length === expected.length && timingSafeEqual(actual, expected);
}
```

The sender resolves DNS again for every attempt, including test sends, and rejects the whole answer set if any address is blocked. It tries checked addresses in resolver order within the request timeout, pinning each connection while verifying HTTPS against the IDNA-encoded hostname. Unspecified, multicast, broadcast, reserved, link-local and metadata addresses are always blocked. Loopback, RFC 1918, shared carrier space, unique-local and site-local IPv6, and documentation and benchmark ranges require `allow_private_network=true`. The setting grants access to private services and should be used only for trusted endpoints. Redirects are never followed. DNS resolution and connection each have a five-second limit; total request timeout is the destination's `timeout_seconds` (1–30 seconds, default 10). Responses are read only up to 64 KiB and their bodies are discarded. Proxy environment variables are ignored.

## SMTP email

SMTP uses Python's standard library in a worker thread. Configure `host`, `port`, `tls_mode`, `from`, and `to` (an array of 1–20 email addresses). `timeout_seconds` covers connect and SMTP commands, accepts 1–30 seconds and defaults to 15. Add `username` and `password` together for a server that requires authentication; the password is encrypted at rest and never returned in an API response. The `from` and `to` fields accept plain email addresses, not display names.

| TLS mode | Typical port | Use |
| --- | --- | --- |
| `starttls` (default) | 587 | Submission server advertising STARTTLS. The adapter sends EHLO, requires STARTTLS, upgrades with certificate verification, sends EHLO again, then authenticates. It never falls back to plaintext. |
| `implicit_tls` | 465 | Submission server requiring TLS from connection start. The adapter verifies the server certificate. |
| `none` | Local relay port | Explicitly configured, unauthenticated plaintext relay. Both username and password are rejected, and the destination must have `allow_private_network: true`. Use only on a trusted local network. |

The container smoke test uses Mailpit with `tls_mode: "none"` on an isolated, internal Compose network. This is test-only configuration; production relays should use verified STARTTLS or implicit TLS where available.

For example, [Amazon SES supports STARTTLS on 587 and implicit TLS on 465](https://docs.aws.amazon.com/ses/latest/dg/smtp-connect.html): use your region's `email-smtp.<region>.amazonaws.com` host, SES SMTP credentials and a verified sender. [Google Workspace documents `smtp-relay.gmail.com` on 587 and 465](https://support.google.com/a/answer/176600); use the mode and authentication settings required by your Workspace relay policy. For an unauthenticated local relay, use its internal host, its configured port, `tls_mode: "none"`, `allow_private_network: true`, and omit both credentials.

SMTP follows the same outbound policy as webhooks:

- The host is resolved once per send. Every resolved address must pass the policy, and the connection goes to the checked address, so a DNS answer that changes after the check is never used. The configured host name is still used for EHLO and for TLS certificate verification.
- Metadata, link-local, multicast and unspecified addresses are always refused (`blocked_destination`).
- Loopback and private ranges (RFC 1918, CGNAT, unique-local IPv6, documentation ranges and IPv6 forms that embed them) are refused unless `allow_private_network` is true.
- Without `allow_private_network`, the port must be 25, 465, 587 or 2525, and `tls_mode: "none"` is rejected, so feedback text never crosses the internet in plaintext. Saving such a destination returns `422 validation_error` for `port` or `tls_mode`. Turning `allow_private_network` off is also rejected while the saved configuration still needs it.

Each mail has `From`, `To`, `Message-ID`, and `X-Feedback-Delivery-Id` headers. Its subject is `[Knowledge Gap]` or `[Knowledge Correction]`, followed by the project name, an em dash and at most 60 characters of the question. Control characters are removed from the subject. The plain-text body contains project, event type, question, correction when present, tags, UTC submission time and occurrence count (`1`). When `ALLOWED_ORIGINS` has an origin, it also links to the delivery dashboard. The HTML alternative escapes every user-supplied field. A **Test** send prefixes the subject with `[TEST]`, starts the body with “This is a test message”, and creates no feedback event.

Temporary connection failures, timeouts and 4xx SMTP replies are retried. 5xx and authentication failures are terminal, as are missing STARTTLS, certificate failures and invalid configuration. Delivery logs contain sanitized categories and a numeric SMTP reply code, never the reply text, recipient addresses or password.
