// M2 check: edit mode on first start, click and Shift+drag selection, undo,
// "开始浏览" queues the cells and switches to browse mode, "编辑方格" goes back.
const { openPage, OFFLINE_DOWNLOADS } = require('./harness.cjs');

(async () => {
  const [shotEdit, shotBrowse] = process.argv.slice(2);
  const { page, problems, close } = await openPage({ settings: OFFLINE_DOWNLOADS });
  const expect = (cond, msg) => { if (!cond) problems.push(msg); };
  // The edit bar is redrawn on the next animation frame, which can take a
  // while with software rendering: wait for the text instead of sleeping.
  const barShows = async (text) => page.waitForFunction(
    (t) => document.getElementById('edit-bar').textContent.includes(t), text, { timeout: 10000 },
  ).then(() => true, () => false);
  try {
    await page.waitForSelector('body[data-map-ready="true"]', { timeout: 30000 });
    expect((await page.getAttribute('body', 'data-mode')) === 'edit', 'starts in edit mode without cells');
    expect((await page.textContent('#btn-mode')) === '开始浏览', 'mode button says 开始浏览');

    await page.evaluate(() => window.__map.jumpTo({ center: [116.0, 39.0], zoom: 5.5 }));
    await page.waitForTimeout(500);
    // Click the cell under the centre of the map.
    const box = await page.locator('#map canvas').boundingBox();
    const cx = box.x + box.width / 2, cy = box.y + box.height / 2;
    await page.mouse.click(cx, cy);
    const one = await barShows('新增 1 格');
    let bar = await page.textContent('#edit-bar');
    expect(one, `one cell selected by click: ${bar}`);

    // Shift+drag a rectangle of cells.
    await page.keyboard.down('Shift');
    await page.mouse.move(cx + 60, cy + 60);
    await page.mouse.down();
    await page.mouse.move(cx + 200, cy + 150, { steps: 5 });
    await page.mouse.up();
    await page.keyboard.up('Shift');
    await page.waitForFunction(() => !document.getElementById('edit-bar').textContent.includes('新增 1 格'), null, { timeout: 10000 })
      .catch(() => {});
    bar = await page.textContent('#edit-bar');
    const added = Number((bar.match(/新增 (\d+) 格/) || [])[1]);
    expect(added > 1, `rectangle selection added cells: ${bar}`);
    const centre = await page.evaluate(() => window.__map.getCenter());
    expect(Math.abs(centre.lng - 116.0) < 0.01, 'map did not pan during Shift+drag');
    if (shotEdit) await page.screenshot({ path: shotEdit });

    // Undo, then select again and apply.
    await page.click('#edit-bar button.undo');
    const cleared = await barShows('新增 0 格');
    bar = await page.textContent('#edit-bar');
    expect(!bar.includes('null'), `no stray text in the edit bar: ${bar}`);
    expect(cleared, `undo clears pending edits: ${bar}`);
    await page.mouse.click(cx, cy);
    await page.click('#btn-mode');
    await page.waitForSelector('body[data-mode="browse"]', { timeout: 5000 });
    expect((await page.textContent('#btn-mode')) === '编辑方格', 'mode button says 编辑方格');
    const cells = await page.evaluate(async () => (await (await fetch(`${location.pathname.split('/app/')[0]}/api/cells`)).json()).cells);
    expect(cells.length === 1 && ['queued', 'downloading'].includes(cells[0].status), `one queued cell: ${JSON.stringify(cells)}`);
    await page.waitForSelector('#download-panel:not([hidden])', { timeout: 5000 });
    expect((await page.textContent('#download-panel')).includes('下载中'), 'download panel shows the queue');
    await page.waitForFunction(() => {
      const map = window.__map;
      return map.loaded() && map.queryRenderedFeatures({ layers: ['cells-browse-hatch'] }).length > 0;
    }, null, { timeout: 10000 }).catch(() => problems.push('queued cell is not drawn in browse mode'));
    expect(!(await page.isVisible('#edit-bar')), 'edit bar is hidden in browse mode');
    if (shotBrowse) await page.screenshot({ path: shotBrowse });

    await page.click('#btn-mode');
    await page.waitForSelector('body[data-mode="edit"]', { timeout: 5000 });
  } finally {
    await close();
  }
  if (problems.length) {
    console.error('FAILED\n' + problems.join('\n'));
    process.exit(1);
  }
  console.log('cells OK');
})();
