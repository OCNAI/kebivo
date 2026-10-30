# Kebivo README demo source

Motion Canvas project behind `../demo.gif`. A customer asks a made-up shop's chatbot "Do you ship to Canada?" and gets "Sorry, I don't know." The app reports that question, and Kebivo counts the repeats. Product UI frames were captured from a fresh `ghcr.io/ocnai/kebivo:0.2.0` container with synthetic data. The chat and end card are drawn. The end card imports the approved Concept C lockup directly from `../logo/lockup.svg`.

| Scene | What it shows |
| --- | --- |
| 1 | A drawn shop assistant chat cannot answer the customer's Canada shipping question |
| 2 | The app's discretionary MCP `report_knowledge_gap` call returns `status: accepted`; this acknowledges intake, not delivery |
| 3 | A configured shipping rule sends the report to the store team webhook, with a real delivered row |
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

`capture.mjs` creates a Shop assistant project, store team webhook destination and shipping routing rule. Its first report uses MCP; the second uses REST. The crops used in the animation exclude the old header mark in the 0.2.0 container UI. No documentation change or automatic fix is depicted.

## Re-capture the README screenshots

Use a separate fresh container so the sample inbox has exactly the eight questions in `capture/readme-shots.mjs`. From this directory, after `npm ci` and with Playwright installed in `frontend/node_modules`:

```sh
docker run -d --name kreadme -p 127.0.0.1:18322:8000 \
  -e SECRET_KEY=$(openssl rand -hex 32) -e ENCRYPTION_KEY=$(openssl rand -hex 32) \
  -e ALLOWED_ORIGINS=http://127.0.0.1:18322 ghcr.io/ocnai/kebivo:0.2.0
printf 'DemoPassw0rd-2026\nDemoPassw0rd-2026\n' | docker exec -i kreadme python -m kebivo.cli bootstrap --email admin@example.org --password-stdin
BASE=http://127.0.0.1:18322 node capture/readme-shots.mjs
node_modules/ffmpeg-static/ffmpeg -loglevel error -y -i shots-readme/raw-inbox.png \
  -vf 'scale=1600:-1,crop=1600:923:0:90' -frames:v 1 ../screenshots/inbox.png
node_modules/ffmpeg-static/ffmpeg -loglevel error -y -i shots-readme/raw-dashboard.png \
  -vf 'scale=1700:-1,crop=1600:621:50:270' -frames:v 1 ../screenshots/dashboard.png
```

Wait for `/health/ready` before bootstrap. The capture creates the Shop assistant project and synthetic reports; it sorts the inbox by occurrence count for the README image.
