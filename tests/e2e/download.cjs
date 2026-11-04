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
      if (Date.now() - started > Number(process.env.E2E_WAIT_MIN || 25) * 60 * 1000) break;
      await page.waitForTimeout(2000);
    }
    console.log(`cell ${cell}: ${row && row.status} after ${Math.round((Date.now() - started) / 1000)} s, ${row && (row.bytes / 1e6).toFixed(1)} MB`, row && row.error ? row.error : '');
    expect(row && row.status === 'cached', 'cell was not cached');
    await page.click('#btn-mode').catch(() => {}); // leave edit mode if needed
    // Views around the cell: overview, inside at z9 and z12.5, and on its east border.
    const [cx, cy] = cell.split('-').map(Number);
    const lon = (x) => (x / 256) * 360 - 180;
    const lat = (y) => (180 / Math.PI) * Math.atan(Math.sinh(Math.PI - (2 * Math.PI * y) / 256));
    const centre = [lon(cx + 0.5), lat(cy + 0.5)];
    const views = [
      ['overview', centre, 6.5],
      ['z9', centre, 9],
      ['z12', centre, 12.5],
      ['border', [lon(cx + 1), lat(cy + 0.5)], 9.5],
    ];
    if (process.env.E2E_VIEWS) for (const v of JSON.parse(process.env.E2E_VIEWS)) views.push(v);
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
