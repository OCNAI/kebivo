# Contributing to Kebivo

Kebivo is a small, self-hosted tool: one container, one SQLite file and no required external service. Keep that setup in mind when proposing changes, and document the behavior you change.

Before you start larger work (a new destination type, a schema change, a new dependency), open an issue to discuss it. Security problems go through [`SECURITY.md`](SECURITY.md), never a public issue.

## Development setup

Pinned toolchain:

| Tool | Version | Source of truth |
| --- | --- | --- |
| Python | 3.12.3 | `.python-version` |
| [uv](https://docs.astral.sh/uv/) | 0.13.1 | `.uv-version` |
| Node.js / npm | 24.14.0 / 11.9.0 | `.nvmrc` |
| Docker with Compose | recent | for `make smoke` and `make e2e` |

Install uv (for example `curl -LsSf https://astral.sh/uv/0.13.1/install.sh | sh` or `pipx install uv==0.13.1`). uv can install the pinned Python with `uv python install 3.12.3`. Then:

```sh
make install       # uv sync --locked --all-extras; npm ci in frontend/
make db-upgrade    # migrate ./data/app.sqlite3
cd frontend && npm run build && cd ..
ALLOWED_ORIGINS=http://127.0.0.1:8000 make dev
uv run python -m kebivo.cli bootstrap --email admin@example.org
```

`make dev` serves the API with auto-reload and the built UI on <http://127.0.0.1:8000/> in `development` mode, using `./data/app.sqlite3`. Rebuild the frontend after UI changes. `cd frontend && npm run dev` starts the Vite dev server for layout work only: it has no API proxy, so sign-in fails there. Without `SECRET_KEY` and `ENCRYPTION_KEY` the development server uses random per-process keys, so tokens and stored secrets stop working after a restart; export both if you want them to persist. `make dev` does not read `.env`.

## Repository layout

| Path | Contents |
| --- | --- |
| `kebivo/` | FastAPI application, worker, destinations, MCP server, CLI. Tests are in `kebivo/tests/`. |
| `migrations/` | Alembic migrations, numbered `NNNN_name.py`. |
| `frontend/` | React admin UI (Vite, TypeScript, Vitest). |
| `e2e/`, `compose.e2e.yaml`, `scripts/e2e.sh` | Acceptance suite against the built container. |
| `examples/` | Runnable REST, MCP and webhook examples. The acceptance suite runs every file. |
| `docs/` | User guides and the webhook payload schema. |
| `scripts/` | Smoke tests, backup and restore, release and upgrade checks, the license inventory. |

## Make targets

| Command | What it does |
| --- | --- |
| `make check` | Everything a change must pass: `lint`, `type`, `test` and `migrations`. Run it before every commit. |
| `make lint` | `ruff check` for Python and ESLint for the frontend. |
| `make format` | `ruff format` and Prettier. |
| `make type` | mypy (strict) and `tsc`. |
| `make test` | pytest and Vitest. |
| `make migrations` | Applies every migration to a temporary SQLite file. |
| `make audit` | Audits the locked third-party Python packages (exported from `uv.lock`, without the local project) and the npm dependencies with `pip-audit` and `npm audit`. Run it when dependencies change. Needs network access. |
| `make docs` | Builds the MkDocs Material site from `docs/` into `site/` with `--strict` (warnings fail) and checks README links. Needs network access the first time to fetch the pinned MkDocs packages. |
| `make smoke` | Builds the image and runs the container smoke test. Run it when the Dockerfile, Compose files or smoke scripts change. Needs Docker, `curl`, `python3`, Node.js with network access and the `claude` CLI. |
| `make e2e` | Builds the image and runs the acceptance suite (AC-01 to AC-15, the examples, restart recovery and a credential canary sweep) online and on an offline network. Run it when anything it covers changes. Needs only Docker; `make e2e E2E_MODES=online` runs one mode. |
| `make release-check VERSION=x.y.z PREVIOUS_REF=<ref>` | Maintainers only: the full local release check. See [releasing](docs/releasing.md). |

No test may need the internet, a paid service or a real model. Use the local fakes: `aiosmtpd` for SMTP, `scripts/smoke-webhook.py` for webhooks and `scripts/fake_llm_server.py` for LLM providers.

## Style

- **Python:** ruff (line length 100) and mypy strict. Type every function. Keep the standard library first; a new runtime dependency needs a discussion.
- **TypeScript:** ESLint and Prettier. Render user-supplied text as text, never as HTML.
- **Logs:** never log feedback content, tokens, passwords, secrets or request bodies. Log identifiers and error categories.
- **Errors:** API errors use the `{"error":{"code","message","details","request_id"}}` envelope and do not echo submitted content.
- **Database:** every schema change is a new Alembic migration with a working downgrade and a test on populated data. Never edit a released migration.
- **Outbound requests** go through the existing SSRF-safe clients; do not add a raw HTTP call.
- Match the surrounding code: naming, comment density and structure.

## Tests

Add tests for the behaviour you change, at the lowest level that proves it, plus an e2e test when it is user-visible through the API. Security-relevant changes (auth, tokens, CSRF, outbound requests, secrets, deletion) need a negative test that shows the protection works.

## Documentation in the same change

When a pull request changes user-visible behaviour, update these files in the same commit:

- the relevant guide in `docs/` and the README when it summarizes that behaviour;
- `.env.example`, `compose.yaml` and the configuration tables in [`docs/self-hosting.md`](docs/self-hosting.md) for a new or changed setting;
- `examples/` and the UI snippets for API changes (the acceptance suite checks that they match);
- `docs/licenses.md` when a dependency changes: run `uv run python scripts/license_inventory.py` (`make check` fails while it is stale);
- `CHANGELOG.md` under an "Unreleased" heading, one sentence per user-visible change, with **Breaking:** for anything that needs operator action.

## Commits and pull requests

- Keep each pull request focused on one change. Describe what changed, why, and how you tested it, and paste the result of `make check` (and `make smoke` or `make e2e` if relevant).
- Write commit messages in the imperative (`Add SMTP timeout setting`).
- Do not commit secrets, `.env` files, databases, real customer data or generated build output. CI runs a secret scan over every commit.
- Only contribute code you wrote or have the right to contribute under the project license. Do not copy code from employers, proprietary products or sources with incompatible licenses.

## Developer Certificate of Origin

Kebivo uses the [Developer Certificate of Origin 1.1](https://developercertificate.org/) instead of a contributor license agreement. There is no CLA. Contributions are accepted under the project license, Apache-2.0. By signing off a commit you certify that you wrote it, or otherwise have the right to submit it under the project's open-source license. Sign off each commit with:

```sh
git commit -s
```

which adds a line such as:

```text
Signed-off-by: Your Name <you@example.org>
```

Use your real name and an address where you can be reached. Maintainers check the sign-off during review.

## License

Kebivo is licensed under the [Apache License 2.0](LICENSE). By contributing you agree that your contribution is licensed under it. The [third-party license inventory](docs/licenses.md) lists every shipped dependency and flags any that need review.
