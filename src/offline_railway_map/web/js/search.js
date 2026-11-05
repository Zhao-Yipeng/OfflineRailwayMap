// Search box (SPEC §6.7): runs on Enter only (no autocomplete).
import { api } from './api.js';
import { $, el } from './ui.js';

const KINDS = { station: '车站', halt: '停靠站', urban: '地铁/轻轨站' };

export function setupSearch(map) {
  const form = $('search-form');
  const input = $('search-input');
  const box = $('search-results');

  const close = () => { box.hidden = true; };
  const note = (text, cls = '') => el('div', { class: `results-note ${cls}` }, text);

  function section(title, items, describe, pick, footer) {
    if (!items) return null;
    const rows = items.length
      ? items.map((item) => el('button', { type: 'button', class: 'result', onclick: () => { pick(item); close(); } },
        el('span', { class: 'result-name' }, item.name || '（无名称）'),
        el('span', { class: 'result-kind' }, describe(item))))
      : [note('没有结果')];
    return el('div', { class: 'results-group' },
      el('div', { class: 'results-title' }, title), ...rows,
      footer ? el('div', { class: 'results-footer' }, footer) : null);
  }

  const flyToPoint = (item, zoom) => map.flyTo({ center: [item.lon, item.lat], zoom, essential: true });
  const flyToPlace = (item) => {
    if (item.bbox) map.fitBounds([[item.bbox[0], item.bbox[1]], [item.bbox[2], item.bbox[3]]], { maxZoom: 14, padding: 60 });
    else flyToPoint(item, 12);
  };

  function render(result) {
    const parts = [
      section('本地车站', result.local, (s) => KINDS[s.kind] || '', (s) => flyToPoint(s, 14)),
    ];
    if (result.offline) {
      parts.push(note('离线，仅显示已缓存区域的车站'));
    } else {
      parts.push(section('车站（OpenRailwayMap）', result.orm, (s) => s.operator || s.kind, (s) => flyToPoint(s, 14),
        'Service by OpenRailwayMap'));
      parts.push(section('地名（Nominatim）', result.places, (p) => p.kind, flyToPlace,
        'Search by Nominatim, Data © OpenStreetMap contributors'));
      for (const message of Object.values(result.errors || {})) parts.push(note(message, 'error'));
    }
    box.replaceChildren(...parts.filter(Boolean));
    box.hidden = false;
  }

  form.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const query = input.value.trim();
    if (!query) { close(); return; }
    box.replaceChildren(note('搜索中…'));
    box.hidden = false;
    try {
      render(await api.get(`/api/search?q=${encodeURIComponent(query)}`));
    } catch (err) {
      box.replaceChildren(note(`搜索失败：${err.message}`, 'error'));
    }
  });
  input.addEventListener('keydown', (ev) => { if (ev.key === 'Escape') { close(); input.blur(); } });
  map.on('mousedown', close);
}
