// Generate the offline basemap layers from the official Protomaps style
// package (SPEC §7.7): "grayscale" flavour, labels in local names (OSM `name`).
//
//   cd <scratch dir> && npm install @protomaps/basemaps@5.7.2
//   node <repo>/tools/build_basemap_style.mjs <scratch dir>/node_modules <repo>
//
// Output: src/offline_railway_map/web/vendor/basemap/grayscale-layers.json
import { createRequire } from 'node:module';
import fs from 'node:fs';
import path from 'node:path';

const [modulesDir, repo] = process.argv.slice(2);
const require = createRequire(path.join(modulesDir, 'noop.js'));
const basemaps = require('@protomaps/basemaps');
const pkg = require('@protomaps/basemaps/package.json');

// Label layers whose text is not a place name keep their own field.
const KEEP_TEXT = new Set(['address_label', 'roads_shields']);
const LOCAL_NAME = ['coalesce', ['get', 'name'], ''];

const layers = basemaps
  .layers('basemap', basemaps.namedFlavor('grayscale'), { lang: 'en' })
  .filter((layer) => layer.id !== 'background') // the page has its own background
  .map((layer) => {
    if (layer.type === 'symbol' && layer.layout && layer.layout['text-field'] && !KEEP_TEXT.has(layer.id)) {
      return { ...layer, layout: { ...layer.layout, 'text-field': LOCAL_NAME } };
    }
    return layer;
  });

const out = path.join(repo, 'src', 'offline_railway_map', 'web', 'vendor', 'basemap');
fs.mkdirSync(out, { recursive: true });
fs.writeFileSync(path.join(out, 'grayscale-layers.json'), JSON.stringify(layers));
fs.writeFileSync(
  path.join(out, 'SOURCE'),
  `Generated from @protomaps/basemaps@${pkg.version} (license: ${pkg.license}), flavor "grayscale", labels = OSM name.\n`,
);
console.log(`${layers.length} layers from @protomaps/basemaps@${pkg.version} (${pkg.license})`);
