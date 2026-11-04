// Layer stack (SPEC §7.1), bottom to top:
//   background, offline basemap, online OSM basemap, cached railways,
//   online ORM tiles, cached labels, cell overlay.
// Each group is closed by an invisible marker layer; a group's layers are
// inserted right before its marker, so modules can add layers in any order.
import { onlineUrl } from './api.js';

const GROUPS = ['basemap', 'osm', 'rail', 'orm', 'labels', 'grid'];

let tileVersion = 0;

export function addGroupMarkers(map) {
  for (const group of GROUPS) {
    map.addLayer({ id: `group-${group}`, type: 'background', layout: { visibility: 'none' }, paint: {} });
  }
}

export function addToGroup(map, group, layer) {
  map.addLayer(layer, `group-${group}`);
}

const ONLINE = {
  osm: { minzoom: 0, maxzoom: 19, paint: { 'raster-saturation': -1, 'raster-fade-duration': 100 } },
  orm: { minzoom: 2, maxzoom: 19, paint: { 'raster-fade-duration': 100 } },
};

function onlineTiles(name) {
  return [onlineUrl(`/tiles/${name}/{z}/{x}/{y}.png?v=${tileVersion}`)];
}

export function addOnlineLayers(map) {
  for (const [name, spec] of Object.entries(ONLINE)) {
    map.addSource(name, {
      type: 'raster',
      tiles: onlineTiles(name),
      tileSize: 256,
      minzoom: spec.minzoom,
      maxzoom: spec.maxzoom,
    });
    addToGroup(map, name, { id: name, type: 'raster', source: name, paint: spec.paint });
  }
}

/** Reload online tiles after the set of cached cells changed (masking differs). */
export function refreshOnlineLayers(map, version) {
  tileVersion = version;
  for (const name of Object.keys(ONLINE)) {
    const source = map.getSource(name);
    if (source) source.setTiles(onlineTiles(name));
  }
}
