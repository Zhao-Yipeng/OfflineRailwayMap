// M5/M6 UI check: legend, settings dialog, clear-cache flow.  No external servers.
const fs = require('fs');
const path = require('path');
const { openPage, OFFLINE_DOWNLOADS } = require('./harness.cjs');

(async () => {
  const shot = process.argv[2];
  const { page, home, problems, close } = await openPage({ settings: OFFLINE_DOWNLOADS });
  const expect = (cond, msg) => { if (!cond) problems.push(msg); };
  try {
    await page.waitForSelector('body[data-map-ready="true"]', { timeout: 30000 });

    // Legend
    await page.click('#btn-legend');
    const legend = await page.textContent('#legend');
    expect(legend.includes('341–360 km/h') && legend.includes('> 360 km/h') && legend.includes('无速度信息'), 'legend lists the speed bands');
    expect(legend.includes('规划（虚线）'), 'legend explains dashed planned lines');
    if (shot) await page.screenshot({ path: `${shot}-legend.png` });
    await page.click('#btn-legend');
    expect(!(await page.isVisible('#legend')), 'legend closes');

    // Settings: change the basemap zoom and save
    await page.click('#btn-settings');
    await page.waitForSelector('.modal .settings');
    expect((await page.textContent('.modal')).includes('与 OpenRailwayMap 项目和 OpenStreetMap 基金会无关'), 'about shows the disclaimer');
    await page.selectOption('#set-zoom', '13');
    if (shot) await page.screenshot({ path: `${shot}-settings.png` });
    await page.click('.modal button.primary');
    await page.waitForSelector('.modal', { state: 'detached' });
    const ini = fs.readFileSync(path.join(home, 'data', 'settings.ini'), 'utf8');
    expect(ini.includes('basemap_maxzoom = 13'), 'settings were saved to data/settings.ini');

    // Queue a cell, then clear everything
    const base = await page.evaluate(() => location.pathname.split('/app/')[0]);
    await page.evaluate(async (b) => {
      await fetch(`${b}/api/cells/apply`, { method: 'POST', body: JSON.stringify({ add: ['210-97'], remove: [] }) });
    }, base);
    await page.waitForTimeout(500);
    await page.click('#btn-clear');
    await page.waitForSelector('.modal');
    expect((await page.textContent('.modal')).includes('程序文件夹将恢复到刚解压时的状态'), 'clear dialog explains the effect');
    await page.click('.modal button.danger');
    await page.waitForSelector('body[data-mode="edit"]', { timeout: 15000 });
    await page.waitForTimeout(500);
    expect(!fs.existsSync(path.join(home, 'data')), 'data folder is gone after clearing');
    const cells = await page.evaluate(async (b) => (await (await fetch(`${b}/api/cells`)).json()).cells, base);
    expect(cells.length === 0, 'no cells after clearing');
    expect(!(await page.isVisible('#download-panel')), 'download panel hidden after clearing');
  } finally {
    await close();
  }
  if (problems.length) {
    console.error('FAILED\n' + problems.join('\n'));
    process.exit(1);
  }
  console.log('features OK');
})();
