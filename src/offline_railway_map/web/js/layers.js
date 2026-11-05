// Layer stack (SPEC §7.1), bottom to top:
//   background, online basemap (OpenFreeMap), mask over cached cells,
//   offline basemap, lines under construction in uncached areas (setting),
//   cached railways, online ORM tiles, cached labels, cell overlay.
// Each group is closed by an invisible marker layer; a group's layers are
// inserted right before its marker, so modules can add layers in any order.
import { onlineUrl } from './api.js';

const GROUPS = ['online', 'mask', 'basemap', 'construction', 'rail', 'orm', 'labels', 'grid'];

let tileVersion = 0;

export function addGroupMarkers(map) {
  for (const group of GROUPS) {
    map.addLayer({ id: `group-${group}`, type: 'background', layout: { visibility: 'none' }, paint: {} });
  }
}

export function addToGroup(map, group, layer) {
  map.addLayer(layer, `group-${group}`);
}

// Online sources, served through the program's online port (SPEC §10.3).
const ONLINE = {
  // OpenFreeMap vector basemap (OpenMapTiles schema); its layers come from basemap.js.
  online: { type: 'vector', path: 'ofm', ext: 'pbf', minzoom: 0, maxzoom: 14 },
  // OpenRailwayMap speed layer.
  orm: {
    type: 'raster', path: 'orm', ext: 'png', minzoom: 2, maxzoom: 19, tileSize: 256,
    layer: { id: 'orm', type: 'raster', source: 'orm', paint: { 'raster-fade-duration': 100 } },
  },
};

function onlineTiles(spec) {
  return [onlineUrl(`/tiles/${spec.path}/{z}/{x}/{y}.${spec.ext}?v=${tileVersion}`)];
}

export function addOnlineLayers(map, basemapLayers) {
  for (const [name, spec] of Object.entries(ONLINE)) {
    const source = { type: spec.type, tiles: onlineTiles(spec), minzoom: spec.minzoom, maxzoom: spec.maxzoom };
    if (spec.tileSize) source.tileSize = spec.tileSize;
    map.addSource(name, source);
  }
  for (const layer of basemapLayers) addToGroup(map, 'online', layer);
  addToGroup(map, 'orm', ONLINE.orm.layer);
}

/** Reload online tiles after the set of cached cells changed (masking differs). */
export function refreshOnlineLayers(map, version) {
  tileVersion = version;
  for (const [name, spec] of Object.entries(ONLINE)) {
    const source = map.getSource(name);
    if (source) source.setTiles(onlineTiles(spec));
  }
}
