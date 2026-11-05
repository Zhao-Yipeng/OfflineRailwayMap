// Map creation (SPEC §6.1): north-up, no rotation or tilt, zoom 2–19,
// Shift+drag reserved for cell selection.
import { url } from './api.js';

export const MIN_ZOOM = 2;
export const MAX_ZOOM = 19;

// CJK labels are drawn with Windows system fonts instead of bundled glyphs (SPEC §7.7).
export const LOCAL_IDEOGRAPH_FONTS = "'Microsoft YaHei UI', 'Microsoft YaHei', 'Yu Gothic UI', 'Malgun Gothic', sans-serif";

function baseStyle() {
  return {
    version: 8,
    glyphs: url('/app/vendor/fonts/{fontstack}/{range}.pbf'),
    sprite: url('/app/vendor/sprites/v4/grayscale'),
    sources: {},
    layers: [
      // Land colour of both basemaps (OpenMapTiles has no land polygons; SPEC §7.2).
      { id: 'background', type: 'background', paint: { 'background-color': '#cccccc' } },
    ],
  };
}

export function createMap(info) {
  const { lon, lat, zoom } = info.view;
  const map = new maplibregl.Map({
    container: 'map',
    style: baseStyle(),
    center: [lon, lat],
    zoom,
    minZoom: MIN_ZOOM,
    maxZoom: MAX_ZOOM,
    maxPitch: 0,
    dragRotate: false,
    pitchWithRotate: false,
    touchPitch: false,
    boxZoom: false,
    renderWorldCopies: false,
    attributionControl: false,
    localIdeographFontFamily: LOCAL_IDEOGRAPH_FONTS,
  });
  map.touchZoomRotate.disableRotation();
  map.keyboard.disableRotation();
  return map;
}
