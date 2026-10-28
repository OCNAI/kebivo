# LLM providers

LLM providers are used for optional semantic deduplication. The default install groups exact repeats and makes no model calls. Add a provider on the **LLM settings** page or through the admin API (`/api/v1/admin/llm-providers`). Question text is sent to a provider only after you select semantic mode and acknowledge its privacy impact.

## Ollama recipe

Install [Ollama](https://ollama.com/) on the host or run it as a service on the same Compose network as Kebivo. Pull an embedding model:

```bash
ollama pull nomic-embed-text
```

Create a `local` provider in **LLM settings** with the base URL `http://host.docker.internal:11434/v1` for host Ollama, or `http://ollama:11434/v1` if your Compose service is named `ollama`. Set its model to `nomic-embed-text`, enable **Allow private network**, and leave the API key empty. The model's vectors have 768 dimensions. If Kebivo and Ollama both run directly on the host, use `http://127.0.0.1:11434/v1` instead. For a container reaching host Ollama on Linux, add `extra_hosts: ["host.docker.internal:host-gateway"]` to the Kebivo service in a Compose override and make Ollama listen on the host gateway interface. The host firewall must allow the container connection. Alternatively, put both services on the same Compose network and use the Ollama service name.

Test the provider with `POST /api/v1/admin/llm-providers/{id}/test?kind=embeddings&dimensions=768`. This authenticated admin test sends fixed synthetic questions, checks that a paraphrase scores above an unrelated question, and reports dimensions, latency, scores and a safe error category. It stores no vectors. Then select semantic **Embeddings** for a project, choose the provider and `nomic-embed-text`, set dimensions to `768`, and acknowledge that question text, including historical representative questions during backfill, may be sent to this provider. See [deduplication](deduplication.md#embeddings-method) for matching and threshold tuning.

## OpenAI-compatible endpoints

Use `openai_compatible` for an embedding endpoint that accepts `POST {base_url}/embeddings` with a model and input array. Set the provider model to an embedding model, supply its API key if required, and set the project's `embedding_model` and `embedding_dimensions` to that endpoint's values. Kebivo sends at most 16 questions per request and validates the returned vector count, dimensions and finite values. Test with `kind=embeddings` before enabling the project. Azure OpenAI embeddings are not supported yet; `azure_openai` providers are rejected for this method.

## Provider settings

| Type | Base URL example | Model / deployment | Key |
| --- | --- | --- | --- |
| `openai_compatible` | `https://api.openai.com/v1` | model name | API key |
| `openai_compatible` (proxy) | `https://proxy.example.com/v1` | proxy model name | API key and optional encrypted extra headers |
| `azure_openai` | `https://example.openai.azure.com` | model name and deployment | API key; `api_version` required |
| `local` (Ollama) | `http://host.docker.internal:11434/v1` | e.g. `llama3.2` | optional |
| `local` (LM Studio) | `http://host.docker.internal:1234/v1` | loaded model name | optional |

Azure requests use `/openai/deployments/{deployment}/chat/completions?api-version={api_version}` and the `api-key` header. Azure requires a deployment and API version; `model` is optional. The other types require a model and use `/chat/completions`; a key, when present, uses Bearer authorization. Extra header names and values are write-only. Never put secrets in the base URL.

`local` providers generally need `allow_private_network: true`. `host-gateway` only adds a name-to-gateway mapping; it does not open the host port or bypass firewall rules. Verify from the app container with `python -c 'import urllib.request; print(urllib.request.urlopen("http://host.docker.internal:11434", timeout=3).status)'` before configuring the provider. Private-network opt-in permits private, loopback and CGNAT addresses but still blocks link-local and cloud metadata addresses. Without opt-in, HTTPS and public addresses are required. DNS is checked and the connection is pinned for each call; redirects are not followed.

Choose `structured_output` per provider: `json_schema` sends a strict JSON schema (default for OpenAI-compatible and Azure), `json_object` asks for a JSON object (default for local), or `prompt_only` sends only the prompt. Every mode validates the returned answer identically. The adapter uses temperature 0, 300 maximum output tokens and a 256 KiB response cap. Configure `timeout_seconds` from 1 to 60 (default 20). `LLM_MAX_CONCURRENCY` accepts 1 to 8 (default 2) for semantic workers.

The worker limits claimed semantic jobs to the available model capacity. Two consecutive timeout, connection, TLS, or HTTP 5xx failures open a per-provider circuit for 60 seconds. While open, semantic evaluations record `provider_unavailable` without a network call; each event keeps its own group and routing continues. A successful response resets the failure count. The circuit is in process memory and resets on worker restart; provider test calls do not share it.

A key can be entered as write-only `api_key` or loaded from an environment variable named `LLM_API_KEY_*` using `api_key_env`. Only names matching `^LLM_API_KEY_[A-Z0-9_]{1,64}$` are accepted. Stored keys take precedence over environment keys. PATCH omits a secret to keep it, sends `null` to remove it, or sends a new string to replace it. Extra headers use the same semantics per header name. API responses indicate configuration and source, never values.

Changing `base_url` or `allow_private_network` clears the stored API key, environment key reference and extra headers. Include the credentials again in the same PATCH if the new destination should receive them.

`POST /api/v1/admin/llm-providers/{id}/test` defaults to `kind=chat` and sends only a fixed synthetic question and three candidates. It reports answer validity, whether the obviously matching candidate was selected, latency and a safe error category. A valid but wrong choice returns a warning. The action is limited to six tests per minute per admin. It does not use customer feedback.

Back up the SQLite database and `ENCRYPTION_KEY` separately. Encrypted provider credentials cannot be recovered from a database backup without the matching key. During rotation, set `ENCRYPTION_KEY_PREVIOUS` to the old key, run `python -m kebivo.cli reencrypt-secrets` to re-encrypt destination and provider credentials, then remove the previous key. Keep both keys out of database backups. Restored installations need the original key or must re-enter provider secrets. Production requires a persistent `ENCRYPTION_KEY`.
