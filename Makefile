.PHONY: docs install dev db-upgrade migrations lint format type test audit smoke e2e e2e-ui check release-check

install:
	uv sync --locked --all-extras
	cd frontend && npm ci --userconfig=/dev/null

dev:
	uv run uvicorn kebivo.app:app --reload --no-access-log

db-upgrade:
	uv run alembic upgrade head

migrations:
	uv run python -c 'import os, subprocess, tempfile; temporary = tempfile.TemporaryDirectory(); os.environ["DATABASE_URL"] = "sqlite:///" + temporary.name + "/migration.sqlite3"; subprocess.run(["alembic", "upgrade", "head"], check=True)'

lint:
	uv run ruff check kebivo e2e examples sdk/python
	cd frontend && npm run lint

format:
	uv run ruff format kebivo
	cd frontend && npm run format

type:
	uv run mypy kebivo
	uv run mypy sdk/python/kebivo
	cd frontend && npm run typecheck

test:
	uv run pytest
	cd frontend && npm test -- --run
	cd sdk/typescript && node --test

audit:
	uv export --locked --no-dev --no-emit-project --format requirements-txt --output-file /tmp/kebivo-python-requirements.txt
	uv run --with pip-audit==2.9.0 pip-audit -r /tmp/kebivo-python-requirements.txt
	cd frontend && npm audit --audit-level=high

smoke:
	bash scripts/container-smoke.sh

# Acceptance suite (AC-01..AC-15, examples, canary sweep), online then offline.
e2e:
	bash scripts/e2e.sh $(E2E_MODES)

# Browser tests (Playwright + Chromium) against the e2e stack; `make e2e-ui SCREENSHOTS=1` also
# regenerates docs/images. First run: cd frontend && npx playwright install chromium
e2e-ui:
	bash scripts/e2e-ui.sh $(if $(SCREENSHOTS),screenshots)

# Static docs site (MkDocs Material, pinned; not a project dependency). Output: site/
docs:
	python3 scripts/check-readme-links.py
	uv run --no-project --with mkdocs==1.6.1 --with mkdocs-material==9.7.7 mkdocs build --strict

check: lint type test migrations

release-check:
	bash scripts/release-check.sh "$(VERSION)" "$(PREVIOUS_REF)"
