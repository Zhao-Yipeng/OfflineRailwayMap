// Page entry point.
import { api, setOnlineOrigin, subscribe } from './api.js';
import {
  addBasemap, loadBasemapStyles, onlineBasemapLayers, onlineBasemapUsable, refreshBasemap, setBasemapMaxzoom,
  setBasemapMode,
} from './basemap.js';
import { Cells, renderDownloadPanel } from './grid.js';
import { setupClearButton } from './clear.js';
import { askAboutExtracts } from './construction.js';
import { setupDetails } from './details.js';
import { addGroupMarkers, addOnlineLayers, refreshOnlineLayers } from './layers.js';
import { setupLegend } from './legend.js';
import { createMap } from './map.js';
import {
  addConstructionLayers, addRailLayers, refreshConstruction, refreshRailLayers, setConstructionVisible,
} from './rail.js';
import { setupSearch } from './search.js';
import { setupSettings } from './settings.js';
import { $, attachScale, renderAttribution, renderStatus, showFatal, toast, trackBottomRow } from './ui.js';

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
  trackBottomRow();
  renderStatus(info.status);
  attachScale(map);
  rememberView(map);

  const [snapshot] = await Promise.all([
    api.get('/api/cells'),
    loadBasemapStyles(),
    new Promise((resolve) => map.on('load', resolve)),
  ]);
  addGroupMarkers(map);
  setBasemapMode(map, onlineBasemapUsable(info.status));
  addBasemap(map, snapshot.basemapMaxzoom);
  addOnlineLayers(map, onlineBasemapLayers());
  addRailLayers(map);
  addConstructionLayers(map, info.settings.alwaysShowConstruction);

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
  setupSearch(map);
  setupDetails(map, cells);
  setupLegend();
  const applySettings = (s) => {
    cells.showOutline = s.showCachedOutline;
    cells.refresh();
    setConstructionVisible(map, s.alwaysShowConstruction);
  };
  setupSettings(applySettings);
  setupClearButton(cells);

  subscribe({
    hello: () => { document.body.dataset.connected = 'true'; },
    status: (status) => {
      renderStatus(status);
      setBasemapMode(map, onlineBasemapUsable(status));
    },
    cells: (snap) => {
      cells.setSnapshot(snap);
      setBasemapMaxzoom(map, snap.basemapMaxzoom);
      renderDownloadPanel(cells);
    },
    cell: (row) => { cells.updateCell(row); renderDownloadPanel(cells); },
    settings: applySettings,
    construction: ({ version }) => refreshConstruction(map, version),
    'construction-question': askAboutExtracts,
    tiles: ({ version }) => {
      refreshOnlineLayers(map, version);
      refreshRailLayers(map, version);
      refreshBasemap(map, version);
    },
  });
  document.body.dataset.mapReady = 'true';
  // A question about the overlay's extracts left unanswered last time (SPEC §7.9).
  api.get('/api/construction').then(({ question }) => askAboutExtracts(question)).catch(() => {});
}

boot().catch((err) => {
  console.error(err);
  showFatal(`程序页面加载失败：${err.message}`);
});
