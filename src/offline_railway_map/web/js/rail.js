// Cached railway lines and stations (SPEC §7.3–7.6, appendices A and B).
// Tiles carry pre-computed properties:
//   st state (o operating, c construction, p proposed, d disused)
//   sp colour speed in km/h (absent = unknown -> grey)
//   lb speed label (only in tiles at or above the label zoom)
//   w  width at z >= 9; lc 'main' | 'line' | 'minor' for widths below z9
//   sk draw order (higher on top); lp label priority (higher first)
// Stations: k 'station' | 'halt' | 'urban', n name.
import { url } from './api.js';
import { addToGroup } from './layers.js';

export const SPEED_BANDS = [
  [10, '#0100CB'], [20, '#001ECB'], [30, '#003DCB'], [40, '#005BCB'], [50, '#007ACB'],
  [60, '#0098CB'], [70, '#00B7CB'], [80, '#00CBC1'], [90, '#00CBA2'], [100, '#00CB84'],
  [110, '#00CB66'], [120, '#00CB47'], [130, '#00CB29'], [140, '#00CB0A'], [150, '#14CB00'],
  [160, '#33CB00'], [170, '#51CB00'], [180, '#70CB00'], [190, '#8ECB00'], [200, '#ADCB00'],
  [210, '#CBCB00'], [220, '#CBAD00'], [230, '#CB8E00'], [240, '#CB7000'], [250, '#CB5100'],
  [260, '#CB3300'], [270, '#CB1400'], [280, '#CB0007'], [290, '#CB0025'], [300, '#CB0044'],
  [320, '#CB0062'], [340, '#CB0081'], [360, '#CB009F'],
];
export const OVER_360 = '#CB00BD';
export const UNKNOWN = '#808080';

function speedColor() {
  const step = ['step', ['get', 'sp'], SPEED_BANDS[0][1]];
  for (let i = 1; i < SPEED_BANDS.length; i++) step.push(SPEED_BANDS[i - 1][0] + 1, SPEED_BANDS[i][1]);
  step.push(361, OVER_360);
  return ['case', ['has', 'sp'], step, UNKNOWN];
}

// Widths (appendix B): 'main' 1.5 / 2.5 / 2.5 / w; 'line' 1.5 / 1.5 / 2.5 / w for
// z2–5 / z6–7 / z8 / z9+; everything else always uses its own width.
function lineWidth(extra = 0) {
  const pick = (main, line) => ['+', ['match', ['get', 'lc'], 'main', main, 'line', line, ['get', 'w']], extra];
  return ['step', ['zoom'], pick(1.5, 1.5), 6, pick(2.5, 1.5), 8, pick(2.5, 2.5), 9, ['+', ['get', 'w'], extra]];
}

let version = 1;
const tileUrl = () => [url(`/tiles/rail/{z}/{x}/{y}.mvt?v=${version}`)];

export function addRailLayers(map) {
  map.addSource('rail', { type: 'vector', tiles: tileUrl(), minzoom: 2, maxzoom: 14 });
  const lineLayout = { 'line-join': 'round', 'line-cap': 'round', 'line-sort-key': ['get', 'sk'] };
  addToGroup(map, 'rail', {
    id: 'rail-casing', type: 'line', source: 'rail', 'source-layer': 'rail',
    layout: lineLayout,
    paint: { 'line-color': '#ffffff', 'line-width': lineWidth(2) },
  });
  addToGroup(map, 'rail', {
    id: 'rail-lines', type: 'line', source: 'rail', 'source-layer': 'rail',
    filter: ['!=', ['get', 'st'], 'p'],
    layout: lineLayout,
    paint: { 'line-color': speedColor(), 'line-width': lineWidth() },
  });
  addToGroup(map, 'rail', {
    id: 'rail-proposed', type: 'line', source: 'rail', 'source-layer': 'rail',
    filter: ['==', ['get', 'st'], 'p'],
    layout: { ...lineLayout, 'line-cap': 'butt' },
    paint: { 'line-color': speedColor(), 'line-width': lineWidth(), 'line-dasharray': [2, 2] },
  });
  addToGroup(map, 'rail', {
    id: 'station-dots', type: 'circle', source: 'rail', 'source-layer': 'stations',
    paint: {
      'circle-radius': ['match', ['get', 'k'], 'station', 3.5, 2.5],
      'circle-color': '#ffffff',
      'circle-stroke-color': '#333333',
      'circle-stroke-width': 1.2,
    },
  });
  addToGroup(map, 'labels', {
    id: 'rail-labels', type: 'symbol', source: 'rail', 'source-layer': 'rail',
    filter: ['has', 'lb'],
    layout: {
      'symbol-placement': 'line',
      'symbol-spacing': 200,
      'symbol-sort-key': ['-', 0, ['get', 'lp']],
      'text-field': ['get', 'lb'],
      'text-font': ['Noto Sans Medium'],
      'text-size': 11,
      'text-max-angle': 35,
      'text-padding': 4,
      'text-rotation-alignment': 'map',
      'text-keep-upright': true,
    },
    paint: { 'text-color': '#111111', 'text-halo-color': '#ffffff', 'text-halo-width': 1 },
  });
  addToGroup(map, 'labels', {
    id: 'station-labels', type: 'symbol', source: 'rail', 'source-layer': 'stations',
    filter: ['!=', ['get', 'n'], ''],
    layout: {
      'text-field': ['get', 'n'],
      'text-font': ['Noto Sans Regular'],
      'text-size': ['match', ['get', 'k'], 'station', 12, 11],
      'text-anchor': 'top',
      'text-offset': [0, 0.6],
      'text-max-width': 8,
      'symbol-sort-key': ['match', ['get', 'k'], 'station', 0, 'halt', 1, 2],
    },
    paint: { 'text-color': '#24292f', 'text-halo-color': '#ffffff', 'text-halo-width': 1.5 },
  });
}

export function refreshRailLayers(map, newVersion) {
  version = newVersion;
  const source = map.getSource('rail');
  if (source) source.setTiles(tileUrl());
}
