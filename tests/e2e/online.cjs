// M1 check: online ORM tiles render through the local proxy; status line
// reports refused services.  Makes a handful of real tile requests (one view).
const { openPage } = require('./harness.cjs');

(async () => {
  const shot = process.argv[2];
  const { page, problems, close } = await openPage();
  try {
    await page.waitForSelector('body[data-map-ready="true"]', { timeout: 30000 });
    await page.evaluate(() => {
      window.__ormTiles = 0;
      window.__map.on('data', (e) => {
        if (e.dataType === 'source' && e.sourceId === 'orm' && e.tile) window.__ormTiles += 1;
      });
      window.__map.jumpTo({ center: [116.38, 39.87], zoom: 10 });
    });
    await page.waitForFunction(() => window.__map.areTilesLoaded(), null, { timeout: 60000 });
    await page.waitForTimeout(1500);
    const status = await page.textContent('#net-status');
    console.log('status line:', status);
    const ormLoaded = await page.evaluate(() => window.__ormTiles);
    console.log('ORM tiles loaded:', ormLoaded);
    if (!ormLoaded) problems.push('no ORM tiles loaded');
    if (shot) await page.screenshot({ path: shot });
  } finally {
    await close();
  }
  if (problems.length) {
    console.error('FAILED\n' + problems.join('\n'));
    process.exit(1);
  }
  console.log('online OK');
})();
