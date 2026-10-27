# Kebivo demo source

Motion Canvas project behind `../demo.gif`, the website's `demo.mp4` and `demo-poster.png`, and `../social-preview.png` (rendered from `../social-preview.html`).
Every product frame is a screenshot of a real `ghcr.io/ocnai/kebivo:0.1.1` container filled with synthetic data
(`example.org`, no real names). Only the chat in scene 1 and the end card are drawn.

| Scene | What it shows |
| --- | --- |
| 1 | A generic chat: the assistant can't find the answer |
| 2 | The agent's `report_knowledge_gap` call over MCP and the real `status: accepted` response |
| 3 | The routing rule and the delivery row marked delivered |
| 4 | Repeats join the same inbox entry; the occurrence count goes 1 to 4 |
| 5 | The owner's note and the resolved status, then the end card |

## Re-render

```sh
cd docs/assets/demo-src
npm install
npm run serve          # editor at http://localhost:9000
```

In the editor, open Video Settings and press Render. It writes PNG frames (1280x720, 30 fps) to `output/project/`
(ignored by git). Then encode the shipped files:

```sh
bash scripts/encode.sh   # docs/assets/demo.gif, plus output/demo.mp4 and output/demo-poster.png for the website
```

## Re-capture the screenshots

```sh
docker run -d --name kdemo -p 127.0.0.1:18321:8000 -v kdemo-data:/data \
  -e SECRET_KEY=$(openssl rand -hex 32) -e ENCRYPTION_KEY=$(openssl rand -hex 32) \
  -e ALLOWED_ORIGINS=http://127.0.0.1:18321 ghcr.io/ocnai/kebivo:0.1.1
printf 'DemoPassw0rd-2026\nDemoPassw0rd-2026\n' | docker exec -i kdemo python -m kebivo.cli bootstrap --email admin@example.org --password-stdin
node capture/capture.mjs   # needs Playwright (frontend/node_modules) and a Chromium; set CHROMIUM=/path if needed
```

`capture.mjs` seeds one project, a webhook destination and a routing rule, sends the MCP call, and writes `shots/*.png`.
It starts the receiver in `capture/receiver.py` inside the container, so use a fresh container each time.
