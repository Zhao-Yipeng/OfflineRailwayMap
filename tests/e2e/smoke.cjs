// M0 smoke test: page loads, map initialises, event stream connects, no errors,
// and nothing is written into the program folder by just opening the page.
const fs = require('fs');
const path = require('path');
const { openPage } = require('./harness.cjs');

(async () => {
  const shot = process.argv[2];
  const { page, home, problems, close } = await openPage();
  try {
    await page.waitForSelector('body[data-map-ready="true"]', { timeout: 30000 });
    await page.waitForSelector('body[data-connected="true"]', { timeout: 10000 });
    const attribution = await page.textContent('#attribution');
    if (!attribution.includes('OpenStreetMap contributors')) problems.push('attribution missing');
    const scale = await page.textContent('#scale .scale-label');
    if (!/\d+ (km|m)/.test(scale)) problems.push(`scale label looks wrong: ${scale}`);
    if (shot) await page.screenshot({ path: shot });
    const leftovers = fs.readdirSync(home);
    if (leftovers.length) problems.push(`program folder not empty: ${leftovers.join(', ')}`);
  } finally {
    await close();
  }
  if (problems.length) {
    console.error('FAILED\n' + problems.join('\n'));
    process.exit(1);
  }
  console.log('smoke OK');
})();
