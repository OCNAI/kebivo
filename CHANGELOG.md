# Changelog

## Unreleased

- Five failed sign-ins now lock one e-mail on one IP address for 15 minutes, not the account everywhere, so someone who only knows the administrator's e-mail can no longer keep them out. Twenty failures from one address lock that address. Failures for one e-mail from many addresses add a delay of up to 60 seconds between attempts. Parallel sign-in requests can no longer slip past the limit together. A throttled sign-in returns `429` with `Retry-After`, and the sign-in page says how long to wait. See [administrator access](docs/self-hosting.md#administrator-access).
- Opening Kebivo at a plain-HTTP LAN address, such as `http://192.168.1.20:8000`, now tells you what works: an HTTPS reverse proxy, or an SSH or VPN tunnel to `127.0.0.1`. The startup error for an `http://` origin in `ALLOWED_ORIGINS` says the same. The self-hosting guide has a new [tunnel section](docs/self-hosting.md#no-certificate-use-a-tunnel) with an `ssh -L` example, and its Nginx example sets `Strict-Transport-Security`.
- Email delivery gives up on a stalled DNS lookup after 5 seconds and retries later, as webhooks already did.
- Scope errors now include `details.required_scope` and tell you to create a new token with that scope; the example MCP client gives one-line token and connection errors without a traceback.

## 0.5.0 — 2026-11-27

Close the loop on a fix: resolve a group with a note on what you changed, see when the question comes back, and get a weekly digest of what is still open.

- Project tokens can read their project's knowledge gaps with the new opt-in `gaps:read` scope, through `GET /api/v1/groups` or the read-only MCP tool `list_open_gaps`. Results contain only the question, count, first and last seen, tags and status; notes, `user_reference` and other report fields are never returned. Reads are limited to 30 a minute per token (`GAPS_READ_RATE_LIMIT_PER_TOKEN`) and each one is audited. The questions are end-user text, so use a separate token for reading and keep `gaps:read` off the token your assistant uses. Existing tokens keep their scopes. A token may now hold only `gaps:read`. See [read open gaps](docs/rest-api.md#read-open-gaps).
- After you resolve a group, the inbox and group page show "Resolved 3 days ago, no new occurrences since". If the question comes back, the group shows a **Reopened** chip with how many new occurrences arrived since you resolved it. The inbox has a **Reopened** filter, and the dashboard counts groups resolved and reopened in the last 7 days, each linking to the filtered inbox.
- Choosing **Resolved** on a group offers an optional "What did you change?" note, shown beside the resolved or reopened chip. Skipping it still resolves the group.
- The group page puts **Status** first, as a row of buttons at the top of Triage, so it is in view without scrolling. Choosing **Resolved** moves focus to the "What did you change?" note. Merge, Split and Find similar groups move under **More actions**, which opens by itself when you select an occurrence to split. Occurrences with identical text show once, with "6 more reports with identical text, last 11 hours ago" and a button to show them all. A report that reopened the group keeps its own card. The Triage panel no longer stays pinned while you scroll, so its lower controls are always reachable.
- On a 390 px phone, the dashboard, inbox and group page fit the screen with no sideways scrolling. Pages no longer jump while they load: the sample-data banner and the page appear together, and the footer stays below the fold until the content arrives. The summary tiles are grouped as Now, Last 7 days and All time, and the delivery-failure tile shows a warning icon, not only a colour, when deliveries failed.
- The grouping hint and the "Report from your own app" guide now say that only identical wording is grouped by default, and that connecting your bot's model as the LLM judge groups paraphrases.
- After setup, the notice says what to do next: open **Do you ship to Canada?** to see seven sample reports collapse into one, then **Connect your own app**. A new project needs only a name; the slug, description, correction feedback and deduplication mode sit under **More options** with the same defaults. After you create a token, the connect code and **Send test event** follow it directly and the project configuration starts folded. The sample-data banner is one short line on desktop as well as on phones.
- Test events from **Send test event** no longer count as real reports: the GitHub star prompt waits for your first real report, and you can still load sample data after sending one.
- New delivery mode: a weekly digest. A webhook, Slack or email destination can send one message a week, on a day and hour you choose, with the top five open groups by occurrences, each with a link, plus how many groups were resolved and reopened. A week with nothing to report sends nothing, and a digest missed while Kebivo was down is sent once on the next start. After an outage that spans several weeks, one digest covers the whole missed period, up to 28 days. It uses the same retries, signing and delivery log as other deliveries. See [weekly digest](docs/destinations.md#weekly-digest).

### Upgrade

Back up first (see [Backup and restore](docs/self-hosting.md#backup-and-restore)). On start, migration `0019` rebuilds the `destinations`, `routing_matches` and `deliveries` tables to allow the weekly digest mode. Existing destinations, routing matches and deliveries are kept, and feedback, groups and tokens are not touched. The latest migration is `0019`, and a 0.4.0 image will not start on a database that has it, so keep the backup if you may need to roll back. Existing tokens keep their scopes; `gaps:read` is only on tokens you create with it. Pull `ghcr.io/ocnai/kebivo:0.5.0` (or `git pull` and rebuild) and check `/health/ready`.

## 0.4.0 — 2026-11-25

First run with no setup: one `docker run` with a named volume gets you to a filled inbox, with no keys to generate and no CLI step.

- A fresh `docker run` with a named volume now generates keys in `/data/kebivo-secrets.env` and prints a one-time administrator setup link (valid for 60 minutes; restart for a new one). Setup can load labelled, removable sample data: a **Shop assistant** project whose rows are marked **Sample**, kept apart from your own feedback, never sent to an LLM provider, and removable from the sample-data banner. Database-only backups exclude the key file; save the matching keys separately before a restore. See [key custody](docs/self-hosting.md#secrets-and-what-to-protect). Compose and existing installations still use keys from the environment.
- First-run and startup errors say what to do next, and invalid settings are printed on one line.
- After your first real report, the Inbox shows administrators a one-time GitHub star prompt. Dismissing it, or following the link, hides it for good.
- The MCP Registry entry no longer lists required environment variables, links to <https://kebivo.com>, and describes who reports a miss. Keys are generated locally, so no external account or secret is needed to start it.
- Inbox filters and text meet contrast checks, and the Connect tab hover state is readable whether or not the tab is selected.
- The README and Docker quickstart lead with the one-command setup and new screenshots, and the reporting guide covers failures and an app-controlled fallback for chatbots.

### Security

Fixes a slow-regex stall in trace-context parsing (GHSA-mf5v-w3w3-f6cg) that any project token could trigger over MCP. On 0.3.0, set `MCP_ENABLED=false` until you upgrade if you share tokens with agents you don't trust.

### Upgrade

Back up first (see [Backup and restore](docs/self-hosting.md#backup-and-restore)). On start, migrations `0017` and `0018` add the sample-data markers and the star prompt dismissal; they leave existing feedback, groups and routing unchanged. The latest migration is `0018`, and a 0.3.0 image will not start on a database that has it, so keep the backup if you may need to roll back. Keep supplying your existing `SECRET_KEY` and `ENCRYPTION_KEY`; an existing database without them still refuses to start. Pull `ghcr.io/ocnai/kebivo:0.4.0` (or `git pull` and rebuild) and check `/health/ready`.

Two behaviour changes:

- **Browser login with `ALLOWED_ORIGINS` empty.** An existing installation now accepts browser login from an HTTP loopback origin when its host and port match the request `Host`. Previously, empty refused all browser login. A valid administrator password is still required. Set `ALLOWED_ORIGINS` for proxy or public access; other origins remain refused. With it empty, the origin check stops other websites, not other hosts, so keep the port bound to `127.0.0.1`.
- **A differing environment key stops startup.** When `/data/kebivo-secrets.env` holds a key and the environment sets that key to a different value, the container now refuses to start and names the variable, instead of starting with a key that breaks project tokens or stored secrets. Passing the same value, or leaving the variable unset or blank, works as before. To change a key on purpose, delete its line from the file first.

## 0.3.0 — 2026-11-04

Opt-in embeddings grouping, steadier intake under bursts, and a readiness healthcheck in the image. Exact grouping stays the default; nothing changes unless you turn embeddings on.

- Optional embeddings grouping (`semantic.method: embeddings`) compares a new question's vector with recent group representatives, using an Ollama or OpenAI-compatible `/embeddings` endpoint you configure. Question text is sent to that endpoint. Dimensions are inferred, input is lowercased, and the default similarity threshold is 0.93. It is precision-first: in calibration with `nomic-embed-text` it grouped about 3 in 100 paraphrases, and a one-word near miss ("VPN" versus "VPS") can score higher than a real paraphrase. Use the LLM judge method when you need paraphrase grouping. See [deduplication](docs/deduplication.md).
- Embeddings tooling: a backfill of recent group representatives when you enable it, an audited preview with question excerpts and scores, a `kind=embeddings` provider test in the API and UI, and `python -m kebivo.cli dedup-eval` to pick a threshold from your own history. Vectors are kept per provider configuration, so vectors from an earlier model, endpoint or key are not compared with new ones. A failed embedding call leaves the event in its own group; intake and routing continue.
- Under SQLite writer or pool contention, feedback intake waits a bounded time and then returns `503 storage_busy` with `Retry-After: 1` instead of a 500. Retry with the same idempotency key. `/health/ready` stays responsive and reports `routing_backlog` (pending count and oldest pending age). `scripts/feedback-burst.py` reproduces a burst on a disposable instance.
- The image has a `/health/ready` healthcheck, so plain `docker run` shows `(healthy)`. It probes every 2 seconds during start-up (Docker Engine 25+) and every 30 seconds after.
- New examples: Microsoft Teams and GitHub Issues webhook relay recipes, LlamaIndex and Pydantic AI MCP examples, and a loader from the JSON Lines export into SQLite.
- The new Kebivo logo in the app, docs and README. The docs site no longer calls Google Fonts or the GitHub API.

### Upgrade

Back up first (see [Backup and restore](docs/self-hosting.md#backup-and-restore)). On start, migrations `0014`–`0016` add the embeddings tables and columns; they leave existing feedback, groups and routing unchanged. The latest migration is `0016`, and a 0.2.0 image will not start on a database that has it, so keep the backup if you may need to roll back. There are no new required settings. Pull `ghcr.io/ocnai/kebivo:0.3.0` (or `git pull` and rebuild) and check `/health/ready`.

## 0.2.0 — 2026-10-27

A new installation now gets from `docker run` to a routed, signed delivery by copy-paste, with no UI step and no model key.

- `python -m kebivo.cli project create --name "..."` creates a project and prints a `feedback:gap` token once (`--json` for scripts). `bootstrap --password-stdin` reads the admin password from a pipe without warnings.
- The quickstart leads with the published image, uses the real clone URL for the build path, and adds a "Route it" step: run `examples/webhook_receiver.py`, add a webhook destination and rule, and see the receiver verify a signed delivery. A test keeps the documented image tag in step with the package version.
- The project Connect panel has a "Send test event" button that sends a sample gap through normal intake and routing and links to its group. Inbox and dashboard empty states point to it.
- Signing in from an origin that is not allowed now returns a message that lists the configured origins instead of a bare "Invalid Origin".
- Native Slack destination: paste a Slack incoming-webhook URL and Kebivo posts a Block Kit message with the question, project, tags, occurrence count and, when `PUBLIC_URL` is set, a link to the group. The URL is validated to `hooks.slack.com`, stored encrypted and never shown again in full.
- Python client (`sdk/python`, standard library only) and TypeScript client (`sdk/typescript`, ESM, no runtime dependencies, Node 20+) for reporting gaps and corrections, with idempotency keys, bounded retries that honour `Retry-After`, and typed errors. An optional LangChain callback reports a gap when a retriever returns no documents. Install from Git; neither is published to a package index.
- Group detail shows when each occurrence was received.

## 0.1.1 — 2026-10-25

Packaging only; no application changes. The MCP Registry server name is now `io.github.OCNAI/kebivo`, in `server.json` and in the image's `io.modelcontextprotocol.server.name` label. The registry matches the GitHub account's letter case, so it refused the lower-case name used in 0.1.0.

## 0.1.0 — 2026-10-25

Kebivo's first open-source release provides local administration, project-scoped REST and MCP feedback intake, exact and optional semantic grouping, inbox triage, routing to webhook, SMTP, and local inbox destinations, durable retry and restart recovery, retention, and project JSON Lines export. The default installation needs no external account or model.

Retention is enabled by default: feedback older than 180 days, terminal delivery attempt details older than 30 days, and audit records older than 90 days are removed. Set the corresponding environment variable to `0` to disable a class. Backups taken before a retention run still contain older data.

Documentation covers the quickstart, a REST integration guide with runnable cURL, Python and TypeScript examples, the MCP guide, a self-hosting guide (settings, TLS, backup and restore, upgrades, credential rotation, troubleshooting), contribution and security policies, and a third-party license inventory. Kebivo is licensed under Apache-2.0; contributions use a Developer Certificate of Origin sign-off and no CLA.

The container image is published as `ghcr.io/ocnai/kebivo:0.1.0` for `linux/amd64` and `linux/arm64`.

Fixed before release: SMTP delivery tries each allowed address of the mail host in resolver order, instead of failing when the first address refuses the connection.
