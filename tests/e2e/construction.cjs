// 「永远显示在建线路」 with China's extracts (SPEC §7.9): switching on downloads the
// provinces, the lines appear, and the page asks whether the extracts downloaded
// for this are deleted.  A stand-in Geofabrik (fake_geofabrik.py) is used.
//
//   PYTHON=/path/to/python node tests/e2e/construction.cjs
const fs = require('fs');
const path = require('path');
const { spawn } = require('child_process');
const { openPage } = require('./harness.cjs');

(async () => {
  const port = 18781;
  const fake = spawn(process.env.PYTHON || 'python3', [path.join(__dirname, 'fake_geofabrik.py'), String(port)],
    { stdio: ['ignore', 'pipe', 'inherit'] });
  let downloads = 0;
  fake.stdout.on('data', (d) => { downloads += String(d).split('\n').filter((l) => l.startsWith('download')).length; });
  await new Promise((r) => setTimeout(r, 1500));
  const settings = ['[general]', 'overpass_endpoints = http://127.0.0.1:9/api/interpreter',
    'basemap_source = http://127.0.0.1:9/none.pmtiles', '[ui]', 'always_show_construction = true',
    'last_view = 116.55,39.72,6', ''].join('\n');
  const env = { OFFLINE_RAILWAY_MAP_EXTRACT_INDEX: `http://127.0.0.1:${port}/index-v1.json` };
  const { page, home, problems, close } = await openPage({ settings, env });
  const expect = (cond, msg) => { if (!cond) problems.push(msg); };
  try {
    await page.waitForFunction(() => document.body.dataset.mapReady === 'true', null, { timeout: 30000 });
    await page.waitForSelector('.modal h2:text("在建线路已准备好")', { timeout: 60000 });
    const text = await page.textContent('.modal');
    expect(text.includes('6 个省级地区'), `the question names the six provinces: ${text}`);
    expect(downloads === 6, `each province downloaded once (${downloads})`);
    await page.click('.modal button:text("立即删除")');
    await page.waitForSelector('.toast:has-text("已删除 6 个数据包")', { timeout: 10000 });
    const left = fs.readdirSync(path.join(home, 'data', 'extracts')).filter((f) => f.endsWith('.sqlite'));
    expect(left.length === 0, `the extracts downloaded for this are gone: ${left}`);
    expect(fs.existsSync(path.join(home, 'data', 'cache', 'construction.sqlite')), 'the lines are kept');
    await page.waitForTimeout(1500);
    const drawn = await page.evaluate(() => window.__map.queryRenderedFeatures({ layers: ['construction-lines'] }).length);
    expect(drawn > 0, 'the line under construction is drawn');
    await page.reload();
    await page.waitForFunction(() => document.body.dataset.mapReady === 'true', null, { timeout: 30000 });
    await page.waitForTimeout(1500);
    expect(!(await page.$('.modal')), 'answered: not asked again');
  } finally {
    await close();
    fake.kill();
  }
  if (problems.length) {
    console.error('FAILED:\n' + problems.join('\n'));
    process.exit(1);
  }
  console.log('construction OK');
})().catch((e) => { console.error(e); process.exit(1); });
