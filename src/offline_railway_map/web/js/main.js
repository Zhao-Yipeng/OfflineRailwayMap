// Page entry point.
import { api, subscribe } from './api.js';
import { createMap } from './map.js';
import { $, attachScale, renderAttribution, showFatal } from './ui.js';

function hasWebGL2() {
  try {
    return !!document.createElement('canvas').getContext('webgl2');
  } catch {
    return false;
  }
}

function rememberView(map) {
  let timer = null;
  map.on('moveend', () => {
    clearTimeout(timer);
    timer = setTimeout(() => {
      const c = map.getCenter();
      api.post('/api/view', { lon: c.lng, lat: c.lat, zoom: map.getZoom() }).catch(() => {});
    }, 500);
  });
}

async function boot() {
  if (!hasWebGL2()) {
    showFatal('显卡不支持 WebGL 2，地图无法显示。请更新显卡驱动后重试。');
    api.post('/api/fatal', { reason: 'webgl2' }).catch(() => {});
    return;
  }
  const info = await api.get('/api/info');
  const map = createMap(info);
  window.__map = map; // handy for debugging and end-to-end tests
  renderAttribution();
  attachScale(map);
  rememberView(map);
  subscribe({ hello: () => { document.body.dataset.connected = 'true'; } });
  map.on('load', () => { document.body.dataset.mapReady = 'true'; });
  $('btn-mode').disabled = true;
  $('btn-clear').disabled = true;
}

boot().catch((err) => {
  console.error(err);
  showFatal(`程序页面加载失败：${err.message}`);
});
