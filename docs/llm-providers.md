# LLM providers

LLM providers are used for semantic deduplication. The default install makes no model calls. Add a provider on the **LLM settings** page or through the admin API (`/api/v1/admin/llm-providers`). Feedback is sent to a provider only after you select semantic mode and a provider for the project.

| Type | Base URL example | Model / deployment | Key |
| --- | --- | --- | --- |
| `openai_compatible` | `https://api.openai.com/v1` | model name | API key |
| `openai_compatible` (proxy) | `https://proxy.example.com/v1` | proxy model name | API key and optional encrypted extra headers |
| `azure_openai` | `https://example.openai.azure.com` | model name and deployment | API key; `api_version` required |
| `local` (Ollama) | `http://host.docker.internal:11434/v1` | e.g. `llama3.2` | optional |
| `local` (LM Studio) | `http://host.docker.internal:1234/v1` | loaded model name | optional |

Azure requests use `/openai/deployments/{deployment}/chat/completions?api-version={api_version}` and the `api-key` header. Azure requires a deployment and API version; `model` is optional. The other types require a model and use `/chat/completions`; a key, when present, uses Bearer authorization. Extra header names and values are write-only. Never put secrets in the base URL.

`local` providers generally need `allow_private_network: true`. On Linux, add `extra_hosts: ["host.docker.internal:host-gateway"]` to the app service in your Compose override, then point the provider to the host port. The local model server must listen on an address the container can reach (usually `0.0.0.0`, not only `127.0.0.1`), and the host firewall must allow connections from the Docker bridge. `host-gateway` only adds a name-to-gateway mapping; it does not open the host port or bypass firewall rules. Verify from the app container with `python -c 'import urllib.request; print(urllib.request.urlopen("http://host.docker.internal:11434", timeout=3).status)'` before configuring the provider. If host access is unavailable, run the model service on the Compose network and use its service name instead. Private-network opt-in permits private, loopback and CGNAT addresses but still blocks link-local and cloud metadata addresses. Without opt-in, HTTPS and public addresses are required. DNS is checked and the connection is pinned for each call; redirects are not followed.

Choose `structured_output` per provider: `json_schema` sends a strict JSON schema (default for OpenAI-compatible and Azure), `json_object` asks for a JSON object (default for local), or `prompt_only` sends only the prompt. Every mode validates the returned answer identically. The adapter uses temperature 0, 300 maximum output tokens and a 256 KiB response cap. Configure `timeout_seconds` from 1 to 60 (default 20). `LLM_MAX_CONCURRENCY` accepts 1 to 8 (default 2) for semantic workers.

The worker limits claimed semantic jobs to the available model capacity. Two consecutive timeout, connection, TLS, or HTTP 5xx failures open a per-provider circuit for 60 seconds. While open, semantic evaluations record `provider_unavailable` without a network call; each event keeps its own group and routing continues. A successful response resets the failure count. The circuit is in process memory and resets on worker restart; provider test calls do not share it.

A key can be entered as write-only `api_key` or loaded from an environment variable named `LLM_API_KEY_*` using `api_key_env`. Only names matching `^LLM_API_KEY_[A-Z0-9_]{1,64}$` are accepted. Stored keys take precedence over environment keys. PATCH omits a secret to keep it, sends `null` to remove it, or sends a new string to replace it. Extra headers use the same semantics per header name. API responses indicate configuration and source, never values.

Changing `base_url` or `allow_private_network` clears the stored API key, environment key reference and extra headers. Include the credentials again in the same PATCH if the new destination should receive them.

`POST /api/v1/admin/llm-providers/{id}/test` sends only a fixed synthetic question and three candidates. It reports answer validity, whether the obviously matching candidate was selected, latency and a safe error category. A valid but wrong choice returns a warning. The action is limited to six tests per minute per admin. It does not use customer feedback.

Back up the SQLite database and `ENCRYPTION_KEY` separately. Encrypted provider credentials cannot be recovered from a database backup without the matching key. During rotation, set `ENCRYPTION_KEY_PREVIOUS` to the old key, run `python -m kebivo.cli reencrypt-secrets` to re-encrypt destination and provider credentials, then remove the previous key. Keep both keys out of database backups. Restored installations need the original key or must re-enter provider secrets. Production requires a persistent `ENCRYPTION_KEY`.
