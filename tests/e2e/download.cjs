// M3 check with real data: download one cell from a public Overpass server,
// then look at it at several zoom levels.  Makes one real Overpass query.
//   node tests/e2e/download.cjs <cell-id> <screenshot-prefix>
const { openPage } = require('./harness.cjs');

(async () => {
  const [cell = '210-97', prefix] = process.argv.slice(2);
  const { page, problems, close } = await openPage();
  const expect = (cond, msg) => { if (!cond) problems.push(msg); };
  try {
    await page.waitForSelector('body[data-map-ready="true"]', { timeout: 30000 });
    const base = await page.evaluate(() => location.pathname.split('/app/')[0]);
    const started = Date.now();
    await page.evaluate(async ([b, id]) => {
      await fetch(`${b}/api/cells/apply`, { method: 'POST', body: JSON.stringify({ add: [id], remove: [] }) });
    }, [base, cell]);
    let row = null;
    for (;;) {
      row = await page.evaluate(async ([b, id]) => {
        const data = await (await fetch(`${b}/api/cells`)).json();
        return data.cells.find((c) => c.id === id);
      }, [base, cell]);
      if (row && (row.status === 'cached' || row.status === 'failed')) break;
      if (Date.now() - started > 25 * 60 * 1000) break;
      await page.waitForTimeout(2000);
    }
    console.log(`cell ${cell}: ${row && row.status} after ${Math.round((Date.now() - started) / 1000)} s, ${row && (row.bytes / 1e6).toFixed(1)} MB`, row && row.error ? row.error : '');
    expect(row && row.status === 'cached', 'cell was not cached');
    await page.click('#btn-mode').catch(() => {}); // leave edit mode if needed
    const views = [
      ['overview', [116.0, 39.4], 6.5],
      ['z9', [116.25, 39.65], 9],
      ['z12', [116.38, 39.865], 12.5],
      ['border', [116.718, 39.6], 9.5],
    ];
    for (const [name, center, zoom] of views) {
      await page.evaluate(([c, z]) => window.__map.jumpTo({ center: c, zoom: z }), [center, zoom]);
      await page.waitForFunction(() => window.__map.areTilesLoaded(), null, { timeout: 60000 });
      await page.waitForTimeout(1200);
      const counts = await page.evaluate(() => ({
        lines: window.__map.queryRenderedFeatures({ layers: ['rail-lines', 'rail-proposed'] }).length,
        labels: window.__map.queryRenderedFeatures({ layers: ['rail-labels'] }).length,
        stations: window.__map.queryRenderedFeatures({ layers: ['station-labels'] }).length,
      }));
      console.log(`${name}: zoom ${zoom}`, JSON.stringify(counts));
      if (prefix) await page.screenshot({ path: `${prefix}-${name}.png` });
    }
  } finally {
    await close();
  }
  if (problems.length) {
    console.error('FAILED\n' + problems.join('\n'));
    process.exit(1);
  }
  console.log('download OK');
})();
