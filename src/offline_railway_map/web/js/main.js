// Page entry point.
import { api, setOnlineOrigin, subscribe } from './api.js';
import { addBasemap, loadBasemapStyle, refreshBasemap, setBasemapMaxzoom } from './basemap.js';
import { Cells, renderDownloadPanel } from './grid.js';
import { addGroupMarkers, addOnlineLayers, refreshOnlineLayers } from './layers.js';
import { createMap } from './map.js';
import { addRailLayers, refreshRailLayers } from './rail.js';
import { $, attachScale, renderAttribution, renderStatus, showFatal, toast } from './ui.js';

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

function setupModeButton(cells) {
  const button = $('btn-mode');
  button.disabled = false;
  button.addEventListener('click', async () => {
    button.disabled = true;
    try {
      if (cells.mode === 'edit') {
        if (await cells.applyEdits()) cells.setMode('browse');
      } else {
        cells.setMode('edit');
      }
    } catch (err) {
      toast(`操作失败：${err.message}`, 'error');
    } finally {
      button.disabled = false;
    }
  });
}

async function boot() {
  if (!hasWebGL2()) {
    showFatal('显卡不支持 WebGL 2，地图无法显示。请更新显卡驱动后重试。');
    api.post('/api/fatal', { reason: 'webgl2' }).catch(() => {});
    return;
  }
  const info = await api.get('/api/info');
  setOnlineOrigin(info.onlineOrigin);
  const map = createMap(info);
  window.__map = map; // handy for debugging and end-to-end tests
  renderAttribution();
  renderStatus(info.status);
  attachScale(map);
  rememberView(map);
  $('btn-clear').disabled = true;

  const [snapshot] = await Promise.all([
    api.get('/api/cells'),
    loadBasemapStyle(),
    new Promise((resolve) => map.on('load', resolve)),
  ]);
  addGroupMarkers(map);
  addBasemap(map, snapshot.basemapMaxzoom);
  addOnlineLayers(map);
  addRailLayers(map);

  const cells = new Cells(map, {
    showOutline: info.settings.showCachedOutline,
    onModeChange: (mode) => {
      $('btn-mode').textContent = mode === 'edit' ? '开始浏览' : '编辑方格';
      document.body.dataset.mode = mode;
      renderDownloadPanel(cells);
    },
  });
  window.__cells = cells;
  cells.setSnapshot(snapshot);
  cells.setMode(cells.hasAnyCells ? 'browse' : 'edit');
  setupModeButton(cells);
  renderDownloadPanel(cells);

  subscribe({
    hello: () => { document.body.dataset.connected = 'true'; },
    status: renderStatus,
    cells: (snap) => {
      cells.setSnapshot(snap);
      setBasemapMaxzoom(map, snap.basemapMaxzoom);
      renderDownloadPanel(cells);
    },
    cell: (row) => { cells.updateCell(row); renderDownloadPanel(cells); },
    tiles: ({ version }) => {
      refreshOnlineLayers(map, version);
      refreshRailLayers(map, version);
      refreshBasemap(map, version);
    },
  });
  document.body.dataset.mapReady = 'true';
}

boot().catch((err) => {
  console.error(err);
  showFatal(`程序页面加载失败：${err.message}`);
});
