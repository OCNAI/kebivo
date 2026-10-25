// Renders docs/assets/social-preview.html to docs/assets/social-preview.png (1280x640).
// Run from frontend/: node ../scripts/render-social-preview.mjs
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';

// Resolve Playwright from frontend/ (the working directory), not from scripts/.
const { chromium } = createRequire(path.join(process.cwd(), 'package.json'))('@playwright/test');

const assets = path.resolve(process.cwd(), '../docs/assets');
const browser = await chromium.launch({ executablePath: process.env.E2E_CHROMIUM_PATH || undefined });
const page = await browser.newPage({ viewport: { width: 1280, height: 640 } });
await page.setContent(readFileSync(path.join(assets, 'social-preview.html'), 'utf8'));
await page.screenshot({ path: path.join(assets, 'social-preview.png') });
await browser.close();
