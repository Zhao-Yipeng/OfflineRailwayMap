// Cell grid, edit/browse modes and cell selection (SPEC §5, §6.2–6.4).
// A cell is one z8 Web Mercator tile; ids are "x-y".
import { api } from './api.js';
import { addToGroup } from './layers.js';
import { $, confirmDialog, el, formatBytes, formatDate, toast } from './ui.js';

const N = 256;
const MAX_LAT = 85.0511287798066;
export const MIN_SELECT_ZOOM = 4;

export const lonToX = (lon) => ((lon + 180) / 360) * N;
export function latToY(lat) {
  const r = (Math.max(-MAX_LAT, Math.min(MAX_LAT, lat)) * Math.PI) / 180;
  return ((1 - Math.log(Math.tan(r) + 1 / Math.cos(r)) / Math.PI) / 2) * N;
}
export const xToLon = (x) => (x / N) * 360 - 180;
export function yToLat(y) {
  const n = Math.PI - (2 * Math.PI * y) / N;
  return (180 / Math.PI) * Math.atan(Math.sinh(n));
}
export const cellId = (x, y) => `${x}-${y}`;
export function parseCellId(id) {
  const [x, y] = id.split('-').map(Number);
  return { x, y };
}
function cellRing(x, y) {
  const w = xToLon(x), e = xToLon(x + 1), n = yToLat(y), s = yToLat(y + 1);
  return [[[w, n], [e, n], [e, s], [w, s], [w, n]]];
}
function cellAt(lngLat) {
  const x = Math.floor(lonToX(lngLat.lng));
  const y = Math.floor(latToY(lngLat.lat));
  return x >= 0 && x < N && y >= 0 && y < N ? { x, y } : null;
}

// Display states (edit mode merges backend status with pending edits).
export const STATE_LABELS = {
  'pending-add': '待下载',
  queued: '下载中',
  downloading: '下载中',
  cached: '已缓存',
  updating: '更新中',
  'pending-remove': '待删除',
  failed: '失败',
};
const COLORS = {
  'pending-add': '#0969da',
  queued: '#bf8700',
  downloading: '#bf8700',
  cached: '#1a7f37',
  updating: '#1a7f37',
  'pending-remove': '#cf222e',
  failed: '#cf222e',
};
const colorMatch = ['match', ['get', 'state'], ...Object.entries(COLORS).flat(), '#666'];
const STAGE_LABELS = { rail: '铁路数据', basemap: '底图', process: '处理' };

function hatchImage() {
  const size = 16;
  const canvas = document.createElement('canvas');
  canvas.width = canvas.height = size;
  const ctx = canvas.getContext('2d');
  ctx.strokeStyle = 'rgba(191, 135, 0, 0.55)';
  ctx.lineWidth = 2;
  for (const offset of [-size, 0, size]) {
    ctx.beginPath();
    ctx.moveTo(offset, size);
    ctx.lineTo(offset + size, 0);
    ctx.stroke();
  }
  return ctx.getImageData(0, 0, size, size);
}

export class Cells {
  constructor(map, { showOutline = true, onModeChange } = {}) {
    this.map = map;
    this.mode = 'browse';
    this.cells = new Map(); // id -> backend row
    this.pendingAdd = new Set();
    this.pendingRemove = new Set();
    this.paused = false;
    this.showOutline = showOutline;
    this.onModeChange = onModeChange || (() => {});
    this._refreshQueued = false;
    this.estimate = null; // { key, bytes, free, sampled } for the pending additions
    this._estimateTimer = null;
    this._estimateKey = '';
    this._setupLayers();
    this._setupInteraction();
  }

  // -- data from the backend -----------------------------------------------------------

  setSnapshot(snapshot) {
    this.cells = new Map(snapshot.cells.map((c) => [c.id, c]));
    this.paused = snapshot.paused;
    for (const id of this.pendingAdd) if (this.cells.has(id) && this.cells.get(id).status !== 'failed') this.pendingAdd.delete(id);
    for (const id of this.pendingRemove) if (!this.cells.has(id)) this.pendingRemove.delete(id);
    this.refresh();
  }

  updateCell(row) {
    this.cells.set(row.id, row);
    this.refresh();
  }

  get hasAnyCells() {
    return this.cells.size > 0;
  }

  isCached(id) {
    const row = this.cells.get(id);
    return !!row && (row.status === 'cached' || row.status === 'updating');
  }

  displayState(id) {
    if (this.pendingAdd.has(id)) return 'pending-add';
    if (this.pendingRemove.has(id)) return 'pending-remove';
    const row = this.cells.get(id);
    return row ? row.status : null;
  }

  // -- modes ---------------------------------------------------------------------------------

  setMode(mode) {
    this.mode = mode;
    const edit = mode === 'edit';
    for (const id of ['cells-edit-fill', 'cells-edit-line', 'grid-lines', 'cell-hover']) {
      this.map.setLayoutProperty(id, 'visibility', edit ? 'visible' : 'none');
    }
    for (const id of ['cells-browse-hatch', 'cells-browse-line']) {
      this.map.setLayoutProperty(id, 'visibility', edit ? 'none' : 'visible');
    }
    if (!edit) this._setHover(null);
    this._updateGrid();
    this.refresh();
    this.onModeChange(mode);
  }

  // Size estimate of the pending additions (SPEC §9.7), refreshed shortly after edits.
  _scheduleEstimate() {
    const key = [...this.pendingAdd].sort().join(',');
    if (key === this._estimateKey) return;
    this._estimateKey = key;
    this.estimate = key ? { key, pending: true } : null;
    clearTimeout(this._estimateTimer);
    if (!key) return;
    this._estimateTimer = setTimeout(async () => {
      try {
        const result = await api.post('/api/estimate', { cells: key.split(',') });
        if (this._estimateKey === key) this.estimate = { key, ...result };
      } catch {
        if (this._estimateKey === key) this.estimate = { key, failed: true };
      }
      this._renderEditBar();
    }, 800);
  }

  _estimateText() {
    const e = this.estimate;
    if (!e) return '';
    if (e.pending) return '（预计大小估算中…）';
    if (e.failed || e.basemapBytes == null) return '（暂时无法估算大小）';
    return `（预计约 ${formatBytes(e.bytes)}${e.sampled ? '，抽样估算' : ''}）`;
  }

  async applyEdits() {
    const add = [...this.pendingAdd];
    const remove = [...this.pendingRemove];
    const e = this.estimate;
    if (add.length && e && !e.pending && !e.failed && e.free != null && e.bytes + 1e9 > e.free) {
      const ok = await confirmDialog({
        title: '磁盘空间可能不足',
        message: `预计需要约 ${formatBytes(e.bytes)}，磁盘剩余 ${formatBytes(e.free)}。\n空间不足 1 GB 时下载会自动暂停。仍要继续吗？`,
        okText: '继续',
      });
      if (!ok) return false;
    }
    if (remove.length) {
      const ok = await confirmDialog({
        title: '删除已缓存的方格',
        message: `将删除 ${remove.length} 个已缓存方格的数据。确定吗？`,
        okText: '删除并开始浏览',
        danger: true,
      });
      if (!ok) return false;
    }
    if (add.length || remove.length) {
      const c = this.map.getCenter();
      const result = await api.post('/api/cells/apply', { add, remove, center: { lon: c.lng, lat: c.lat } });
      this.pendingAdd.clear();
      this.pendingRemove.clear();
      this.setSnapshot(result);
    }
    return true;
  }

  discardEdits() {
    this.pendingAdd.clear();
    this.pendingRemove.clear();
    this.refresh();
  }

  toggle(id) {
    const row = this.cells.get(id);
    if (this.pendingAdd.delete(id) || this.pendingRemove.delete(id)) {
      // undo a pending edit
    } else if (!row || row.status === 'failed') {
      this.pendingAdd.add(id);
    } else {
      this.pendingRemove.add(id);
    }
    this.refresh();
  }

  selectRect(x0, y0, x1, y1, add) {
    for (let x = Math.min(x0, x1); x <= Math.max(x0, x1); x++) {
      for (let y = Math.min(y0, y1); y <= Math.max(y0, y1); y++) {
        const id = cellId(x, y);
        const row = this.cells.get(id);
        if (add) {
          this.pendingRemove.delete(id);
          if (!row || row.status === 'failed') this.pendingAdd.add(id);
        } else {
          this.pendingAdd.delete(id);
          if (row && row.status !== 'failed') this.pendingRemove.add(id);
        }
      }
    }
    this.refresh();
  }

  // -- rendering -----------------------------------------------------------------------------

  _setupLayers() {
    const map = this.map;
    const empty = { type: 'FeatureCollection', features: [] };
    map.addImage('cell-hatch', hatchImage());
    map.addSource('cells', { type: 'geojson', data: empty });
    map.addSource('cell-points', { type: 'geojson', data: empty });
    map.addSource('grid', { type: 'geojson', data: empty });
    map.addSource('cell-hover', { type: 'geojson', data: empty });
    addToGroup(map, 'grid', {
      id: 'cells-edit-fill', type: 'fill', source: 'cells',
      paint: {
        'fill-color': colorMatch,
        'fill-opacity': ['match', ['get', 'state'], 'failed', 0.1, ['pending-add', 'pending-remove'], 0.3, 0.2],
      },
    });
    addToGroup(map, 'grid', {
      id: 'grid-lines', type: 'line', source: 'grid', minzoom: MIN_SELECT_ZOOM,
      paint: { 'line-color': '#57606a', 'line-width': 0.6, 'line-opacity': 0.7 },
    });
    addToGroup(map, 'grid', {
      id: 'cells-edit-line', type: 'line', source: 'cells',
      paint: {
        'line-color': colorMatch,
        'line-width': 1.4,
        'line-dasharray': ['match', ['get', 'state'], 'pending-remove', ['literal', [3, 2]], ['literal', [1, 0]]],
      },
    });
    addToGroup(map, 'grid', {
      id: 'cells-browse-hatch', type: 'fill', source: 'cells',
      filter: ['in', ['get', 'state'], ['literal', ['queued', 'downloading']]],
      paint: { 'fill-pattern': 'cell-hatch' },
    });
    addToGroup(map, 'grid', {
      id: 'cells-browse-line', type: 'line', source: 'cells',
      filter: ['in', ['get', 'state'], ['literal', ['cached', 'updating', 'failed', 'queued', 'downloading']]],
      paint: {
        'line-color': colorMatch,
        'line-width': ['match', ['get', 'state'], 'failed', 1.6, 0.8],
        'line-opacity': ['match', ['get', 'state'], ['cached', 'updating'], 0.55, 0.9],
      },
    });
    addToGroup(map, 'grid', {
      id: 'cell-hover', type: 'line', source: 'cell-hover',
      paint: { 'line-color': '#1f2328', 'line-width': 2 },
    });
    addToGroup(map, 'grid', {
      id: 'cell-progress', type: 'symbol', source: 'cell-points', minzoom: 5,
      layout: {
        'text-field': ['get', 'label'],
        'text-font': ['Noto Sans Medium'],
        'text-size': 12,
        'text-allow-overlap': false,
      },
      paint: { 'text-color': '#6f4e00', 'text-halo-color': '#ffffff', 'text-halo-width': 1.5 },
    });
    map.on('moveend', () => this._updateGrid());
  }

  refresh() {
    if (this._refreshQueued) return;
    this._refreshQueued = true;
    requestAnimationFrame(() => {
      this._refreshQueued = false;
      this._render();
    });
  }

  _render() {
    const ids = new Set([...this.cells.keys(), ...this.pendingAdd]);
    const polygons = [];
    const points = [];
    for (const id of ids) {
      const state = this.displayState(id);
      if (!state) continue;
      if (this.mode === 'browse' && state === 'cached' && !this.showOutline) continue;
      const { x, y } = parseCellId(id);
      polygons.push({ type: 'Feature', properties: { id, state }, geometry: { type: 'Polygon', coordinates: cellRing(x, y) } });
      const row = this.cells.get(id);
      if (row && ['queued', 'downloading', 'updating'].includes(row.status)) {
        const label = row.status === 'queued' ? '等待下载' : `${Math.round((row.progress || 0) * 100)}%`;
        points.push({
          type: 'Feature', properties: { label },
          geometry: { type: 'Point', coordinates: [xToLon(x + 0.5), yToLat(y + 0.5)] },
        });
      }
    }
    this.map.getSource('cells').setData({ type: 'FeatureCollection', features: polygons });
    this.map.getSource('cell-points').setData({ type: 'FeatureCollection', features: points });
    this._renderEditBar();
  }

  _updateGrid() {
    const source = this.map.getSource('grid');
    if (this.mode !== 'edit' || this.map.getZoom() < MIN_SELECT_ZOOM) {
      source.setData({ type: 'FeatureCollection', features: [] });
      return;
    }
    const b = this.map.getBounds();
    const x0 = Math.max(0, Math.floor(lonToX(b.getWest())));
    const x1 = Math.min(N, Math.ceil(lonToX(b.getEast())));
    const y0 = Math.max(0, Math.floor(latToY(b.getNorth())));
    const y1 = Math.min(N, Math.ceil(latToY(b.getSouth())));
    const north = yToLat(y0), south = yToLat(y1), west = xToLon(x0), east = xToLon(x1);
    const lines = [];
    for (let x = x0; x <= x1; x++) lines.push([[xToLon(x), north], [xToLon(x), south]]);
    for (let y = y0; y <= y1; y++) lines.push([[west, yToLat(y)], [east, yToLat(y)]]);
    source.setData({ type: 'Feature', properties: {}, geometry: { type: 'MultiLineString', coordinates: lines } });
  }

  _setHover(cell) {
    const source = this.map.getSource('cell-hover');
    const tip = $('cell-tip');
    if (!cell) {
      source.setData({ type: 'FeatureCollection', features: [] });
      tip.hidden = true;
      this._hoverId = null;
      return;
    }
    const id = cellId(cell.x, cell.y);
    if (id !== this._hoverId) {
      this._hoverId = id;
      source.setData({ type: 'Feature', properties: {}, geometry: { type: 'Polygon', coordinates: cellRing(cell.x, cell.y) } });
    }
    const state = this.displayState(id);
    const row = this.cells.get(id);
    const parts = [`方格 ${id}`, state ? STATE_LABELS[state] : '未选'];
    if (row && row.railAt) parts.push(`下载于 ${formatDate(row.railAt)}`);
    if (row && row.bytes) parts.push(formatBytes(row.bytes));
    if (row && row.error) parts.push(`错误：${row.error}`);
    tip.textContent = parts.join(' · ');
    tip.hidden = false;
  }

  _renderEditBar() {
    const bar = $('edit-bar');
    if (this.mode !== 'edit') {
      bar.hidden = true;
      return;
    }
    let cached = 0, bytes = 0;
    for (const row of this.cells.values()) {
      if (row.status === 'cached' || row.status === 'updating') { cached += 1; bytes += row.bytes || 0; }
    }
    const changes = this.pendingAdd.size + this.pendingRemove.size;
    const updateAll = async () => {
      const ok = await confirmDialog({
        title: '全部更新铁路数据',
        message: `将重新下载全部 ${cached} 个已缓存方格的铁路数据（底图不变）。更新期间继续显示旧数据。`,
        okText: '开始更新',
      });
      if (ok) api.post('/api/cells/update-all').catch((e) => toast(e.message, 'error'));
    };
    this._scheduleEstimate();
    bar.replaceChildren(...[
      el('span', {}, `新增 ${this.pendingAdd.size} 格${this._estimateText()} · 将删除 ${this.pendingRemove.size} 格 · 已缓存 ${cached} 格（共 ${formatBytes(bytes)}）`),
      changes ? el('button', { type: 'button', class: 'link undo', onclick: () => this.discardEdits() }, '撤销本次更改') : null,
      cached ? el('button', { type: 'button', class: 'link', onclick: updateAll }, '全部更新铁路数据') : null,
      el('span', { class: 'hint' }, '单击选择 · Shift+拖动框选 · Alt+拖动取消 · 右键已缓存方格可更新或删除'),
    ].filter(Boolean));
    bar.hidden = false;
  }

  // -- interaction -------------------------------------------------------------------------

  _setupInteraction() {
    const map = this.map;
    const canvas = map.getCanvasContainer();
    let drag = null;

    const modifiers = (ev) => ev.shiftKey || ev.altKey;
    const syncDragPan = (ev) => {
      if (this.mode === 'edit' && modifiers(ev)) map.dragPan.disable();
      else if (!drag) map.dragPan.enable();
    };
    window.addEventListener('keydown', syncDragPan);
    window.addEventListener('keyup', syncDragPan);
    window.addEventListener('blur', () => { if (!drag) map.dragPan.enable(); });

    map.on('mousemove', (e) => {
      if (this.mode !== 'edit' || map.getZoom() < MIN_SELECT_ZOOM) {
        this._setHover(null);
        return;
      }
      this._setHover(cellAt(e.lngLat));
      const tip = $('cell-tip');
      tip.style.left = `${e.point.x + 14}px`;
      tip.style.top = `${e.point.y + 14}px`;
    });
    map.on('mouseout', () => this._setHover(null));

    map.on('click', (e) => {
      if (this.mode !== 'edit') return;
      if (map.getZoom() < MIN_SELECT_ZOOM) {
        toast('请放大后再选择方格');
        return;
      }
      const cell = cellAt(e.lngLat);
      if (cell) this.toggle(cellId(cell.x, cell.y));
    });

    canvas.addEventListener('mousedown', (ev) => {
      if (this.mode !== 'edit' || ev.button !== 0 || !modifiers(ev) || map.getZoom() < MIN_SELECT_ZOOM) return;
      ev.preventDefault();
      ev.stopPropagation();
      map.dragPan.disable();
      const rect = canvas.getBoundingClientRect();
      const start = { x: ev.clientX - rect.left, y: ev.clientY - rect.top };
      const box = el('div', { class: `select-box ${ev.altKey ? 'remove' : 'add'}` });
      canvas.append(box);
      drag = { start, box, add: !ev.altKey, rect };
      const move = (mv) => {
        const x = mv.clientX - rect.left, y = mv.clientY - rect.top;
        Object.assign(box.style, {
          left: `${Math.min(x, start.x)}px`, top: `${Math.min(y, start.y)}px`,
          width: `${Math.abs(x - start.x)}px`, height: `${Math.abs(y - start.y)}px`,
        });
      };
      const up = (upEv) => {
        window.removeEventListener('mousemove', move);
        window.removeEventListener('mouseup', up);
        box.remove();
        const end = { x: upEv.clientX - rect.left, y: upEv.clientY - rect.top };
        const a = cellAt(map.unproject([start.x, start.y]));
        const b = cellAt(map.unproject([end.x, end.y]));
        if (a && b && (Math.abs(end.x - start.x) > 3 || Math.abs(end.y - start.y) > 3)) {
          this.selectRect(a.x, a.y, b.x, b.y, drag.add);
        }
        drag = null;
        syncDragPan(upEv);
      };
      window.addEventListener('mousemove', move);
      window.addEventListener('mouseup', up);
    }, true);

    map.on('contextmenu', (e) => {
      if (this.mode !== 'edit') return;
      const cell = cellAt(e.lngLat);
      if (!cell) return;
      const id = cellId(cell.x, cell.y);
      const row = this.cells.get(id);
      if (!row) return;
      e.originalEvent.preventDefault();
      this._contextMenu(e.originalEvent, id, row);
    });
  }

  _contextMenu(ev, id, row) {
    const items = [];
    if (row.status === 'cached') {
      items.push(['更新铁路数据', () => api.post('/api/cells/update', { id, basemap: false })]);
      items.push(['更新铁路数据和底图', () => api.post('/api/cells/update', { id, basemap: true })]);
    }
    if (row.status === 'failed') items.push(['重试', () => api.post('/api/cells/retry', { ids: [id] })]);
    if (['cached', 'updating', 'failed'].includes(row.status)) {
      items.push(['删除此格', async () => {
        const ok = await confirmDialog({ title: '删除此格', message: `将删除方格 ${id} 的全部数据。确定吗？`, okText: '删除', danger: true });
        if (ok) await api.post('/api/cells/delete', { ids: [id] });
      }]);
    }
    if (!items.length) return;
    const menu = $('context-menu');
    const close = () => { menu.hidden = true; document.removeEventListener('mousedown', outside, true); };
    const outside = (e) => { if (!menu.contains(e.target)) close(); };
    menu.replaceChildren(
      el('div', { class: 'menu-title' }, `方格 ${id}`),
      ...items.map(([label, fn]) => el('button', {
        type: 'button',
        onclick: async () => {
          close();
          try { await fn(); } catch (err) { toast(err.message, 'error'); }
        },
      }, label)),
    );
    menu.style.left = `${ev.clientX}px`;
    menu.style.top = `${ev.clientY}px`;
    menu.hidden = false;
    setTimeout(() => document.addEventListener('mousedown', outside, true));
  }
}

export function downloadSummary(cells) {
  let active = 0, cached = 0, failed = 0, bytes = 0, current = null;
  for (const row of cells.cells.values()) {
    if (['queued', 'downloading', 'updating'].includes(row.status) && (row.status !== 'updating' || row.updateKind)) active += 1;
    if (row.status === 'cached') cached += 1;
    if (row.status === 'failed') failed += 1;
    if (row.status === 'downloading' || (row.status === 'updating' && row.stage)) current = row;
    bytes += row.bytes || 0;
  }
  return { active, cached, failed, bytes, current };
}

// Rough remaining time from the cells finished during this session (SPEC §6.5).
const finished = [];
let lastCached = null;
function remainingText(cached, active) {
  const now = Date.now();
  if (lastCached !== null && cached > lastCached) for (let i = lastCached; i < cached; i++) finished.push(now);
  lastCached = cached;
  if (finished.length < 2 || !active) return '';
  const recent = finished.slice(-20);
  const perCell = (recent[recent.length - 1] - recent[0]) / (recent.length - 1);
  const minutes = Math.round((perCell * active) / 60000);
  if (minutes < 1) return ' · 剩余不到 1 分钟';
  return minutes < 120 ? ` · 约剩 ${minutes} 分钟` : ` · 约剩 ${Math.round(minutes / 60)} 小时`;
}

export function renderDownloadPanel(cells) {
  const panel = $('download-panel');
  const { active, cached, failed, bytes, current } = downloadSummary(cells);
  const eta = remainingText(cached, active);
  if (!active && !failed) {
    panel.hidden = true;
    return;
  }
  const parts = [];
  if (active) {
    const head = cells.paused ? '已暂停' : '下载中';
    parts.push(el('span', { class: 'strong' }, `${head} ${cached}/${cached + active}`));
    if (current && !cells.paused) {
      const stage = STAGE_LABELS[current.stage] || '准备';
      parts.push(el('span', {}, ` · ${current.id} ${stage} ${Math.round((current.progress || 0) * 100)}%`));
    }
    parts.push(el('span', { class: 'muted' }, ` · 已下载 ${formatBytes(bytes)}${cells.paused ? '' : eta}`));
    parts.push(el('button', {
      type: 'button',
      onclick: () => api.post('/api/downloads/pause', { paused: !cells.paused }).catch((e) => toast(e.message, 'error')),
    }, cells.paused ? '继续' : '暂停'));
  }
  if (failed) {
    const failedIds = [...cells.cells.values()].filter((r) => r.status === 'failed').map((r) => r.id);
    parts.push(el('span', { class: 'failed' }, ` 失败 ${failed} 格`));
    parts.push(el('button', {
      type: 'button',
      onclick: () => api.post('/api/cells/retry', { ids: failedIds }).catch((e) => toast(e.message, 'error')),
    }, '全部重试'));
  }
  panel.replaceChildren(...parts);
  panel.hidden = false;
}
