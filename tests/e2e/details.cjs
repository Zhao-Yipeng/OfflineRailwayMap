// Click details: the OSM ways of one line (two tracks, or split at a bridge) are
// one entry, with the differing tags and a switch between the ways (SPEC §6.8).
// A stand-in Overpass (fake_lines.py) answers the online details query.
//
//   PYTHON=/path/to/python node tests/e2e/details.cjs
const path = require('path');
const { spawn } = require('child_process');
const { openPage } = require('./harness.cjs');

async function run(mode, port) {
  const fake = spawn(process.env.PYTHON || 'python3', ['-I', path.join(__dirname, 'fake_lines.py'), String(port), mode], { stdio: 'ignore' });
  await new Promise((r) => setTimeout(r, 800));
  const settings = ['[general]', `overpass_endpoints = http://127.0.0.1:${port}/api/interpreter`,
    'basemap_source = http://127.0.0.1:9/none.pmtiles', '[ui]', 'last_view = 116.0,39.5,12', ''].join('\n');
  const { page, problems, close } = await openPage({ settings });
  const out = {};
  try {
    await page.waitForFunction(() => document.body.dataset.mapReady === 'true', null, { timeout: 30000 });
    await page.click('#btn-mode');
    await page.waitForFunction(() => document.body.dataset.mode === 'browse', null, { timeout: 10000 });
    await page.mouse.click(640, 400);
    await page.waitForSelector('.details-popup .details', { timeout: 15000 });
    out.first = await page.textContent('.details-popup');
    if (mode === 'three') {
      out.entries = await page.$$eval('.details-popup .result-name', (els) => els.map((e) => e.textContent));
      await page.click('.details-popup .result >> nth=0');
      await page.waitForSelector('.details-popup .details-segments');
      out.first = await page.textContent('.details-popup');
    }
    out.segments = await page.textContent('.details-popup .details-segments');
    await page.click('.details-popup .segment-buttons button >> nth=1');
    await page.waitForTimeout(300);
    out.second = await page.textContent('.details-popup');
    out.problems = problems;
  } finally {
    await close();
    fake.kill();
  }
  return out;
}

(async () => {
  const problems = [];
  const expect = (cond, msg) => { if (!cond) problems.push(msg); };
  const two = await run('two', 18771);
  expect(!two.first.includes('此处有'), 'two ways of one line: no list, the details straight away');
  expect(two.segments.includes('此处同一线路有 2 段') && two.segments.includes('railway:track_ref'), 'the differing tag is named');
  expect(/股道号I(?!I)/.test(two.second.replace(/\s+/g, '')), 'the switch shows the other way');
  const three = await run('three', 18772);
  expect(JSON.stringify(three.entries) === JSON.stringify(['京雄城际线 · 3085 · 80 km/h（2 段）', '京沪线 · 0002 · 160 km/h']),
    `one entry per line: ${JSON.stringify(three.entries)}`);
  problems.push(...two.problems, ...three.problems);
  if (problems.length) {
    console.error('FAILED:\n' + problems.join('\n'));
    process.exit(1);
  }
  console.log('details OK');
})().catch((e) => { console.error(e); process.exit(1); });
