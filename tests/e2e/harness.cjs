// End-to-end harness: starts the backend with `--serve` in a throw-away
// program folder and opens the page in Chromium (Playwright).
//
//   PYTHON=/path/to/python node tests/e2e/smoke.cjs
//
// Not part of the pytest run; used during development on any OS.
const { spawn } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { chromium } = require('playwright');

const ROOT = path.resolve(__dirname, '..', '..');

async function startBackend(homeDir, extraEnv = {}) {
  const python = process.env.PYTHON || 'python3';
  const child = spawn(python, ['-m', 'offline_railway_map', '--serve'], {
    cwd: ROOT,
    env: {
      ...process.env,
      PYTHONPATH: path.join(ROOT, 'src'),
      OFFLINE_RAILWAY_MAP_HOME: homeDir,
      ...extraEnv,
    },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  let stderr = '';
  child.stderr.on('data', (d) => { stderr += d; });
  const url = await new Promise((resolve, reject) => {
    let out = '';
    const timer = setTimeout(() => reject(new Error(`backend did not start:\n${stderr}`)), 20000);
    child.stdout.on('data', (d) => {
      out += d;
      const line = out.split('\n').find((l) => l.startsWith('http://'));
      if (line) { clearTimeout(timer); resolve(line.trim()); }
    });
    child.on('exit', (code) => reject(new Error(`backend exited (${code}):\n${stderr}`)));
  });
  return { child, url, stderr: () => stderr };
}

// Settings that keep UI tests away from real Overpass / Protomaps servers.
const OFFLINE_DOWNLOADS = [
  '[general]',
  'overpass_endpoints = http://127.0.0.1:9/api/interpreter',
  'basemap_source = http://127.0.0.1:9/none.pmtiles',
  '',
].join('\n');

async function openPage({ homeDir, viewport = { width: 1280, height: 800 }, env, settings } = {}) {
  const home = homeDir || fs.mkdtempSync(path.join(os.tmpdir(), 'orm-e2e-'));
  if (settings) {
    fs.mkdirSync(path.join(home, 'data'), { recursive: true });
    fs.writeFileSync(path.join(home, 'data', 'settings.ini'), settings);
  }
  const backend = await startBackend(home, env);
  const browser = await chromium.launch({ args: ['--use-angle=swiftshader', '--enable-unsafe-swiftshader'] });
  const page = await browser.newPage({ viewport, deviceScaleFactor: 1 });
  const problems = [];
  page.on('console', (msg) => { if (msg.type() === 'error') problems.push(`console: ${msg.text()}`); });
  page.on('pageerror', (err) => problems.push(`pageerror: ${err.message}`));
  await page.goto(backend.url);
  const close = async () => {
    await browser.close();
    backend.child.kill('SIGINT');
    await new Promise((r) => setTimeout(r, 300));
  };
  return { page, backend, home, problems, close };
}

module.exports = { openPage, startBackend, ROOT, OFFLINE_DOWNLOADS };
