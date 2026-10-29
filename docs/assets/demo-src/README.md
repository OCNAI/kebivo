# Kebivo README demo source

Motion Canvas project behind `../demo.gif`. The story uses the same `/orders` question as the README Quickstart. Product UI frames were captured from a fresh `ghcr.io/ocnai/kebivo:0.2.0` container with synthetic data. The chat and end card are drawn. The end card imports the approved Concept C lockup directly from `../logo/lockup.svg`.

| Scene | What it shows |
| --- | --- |
| 1 | A drawn assistant chat cannot answer the `/orders` pagination question |
| 2 | The app's discretionary MCP `report_knowledge_gap` call returns `status: accepted`; this acknowledges intake, not delivery |
| 3 | A configured routing rule sends the report to the API docs webhook, with a real delivered row |
| 4 | A second, deterministic REST submission of the same question joins the existing group, raising its count from 1 to 2 |
| 5 | End card with the approved lockup and the visible value proposition |

## Re-render

```sh
cd docs/assets/demo-src
npm ci
npm run serve          # editor at http://localhost:9000
```

In the editor, open Video Settings and press Render. It writes PNG frames (1280x720, 30 fps) to `output/project/` (ignored by git). Then encode the shipped README GIF:

```sh
bash scripts/encode.sh
```

The encoder also writes `output/demo.mp4` and `output/demo-poster.png` as local review files. Those files are not published to the website by this change.

## Re-capture the product frames

Use a fresh container and a free port 18321. This script seeds only synthetic `example.org` data and writes the screenshots to `shots/`. It uses Playwright from `frontend/node_modules` and Chromium; set `CHROMIUM=/path` if needed.

```sh
docker run -d --name kdemo -p 127.0.0.1:18321:8000 -v kdemo-data:/data \
  -e SECRET_KEY=$(openssl rand -hex 32) -e ENCRYPTION_KEY=$(openssl rand -hex 32) \
  -e ALLOWED_ORIGINS=http://127.0.0.1:18321 ghcr.io/ocnai/kebivo:0.2.0
printf 'DemoPassw0rd-2026\nDemoPassw0rd-2026\n' | docker exec -i kdemo python -m kebivo.cli bootstrap --email admin@example.org --password-stdin
node capture/capture.mjs
```

`capture.mjs` creates an API Docs Assistant project, webhook destination and routing rule. Its first report uses MCP; the second uses REST. The crops used in the animation exclude the old header mark in the 0.2.0 container UI. No documentation change or automatic fix is depicted.
