// Line details on click in browse mode (SPEC §6.8).
import { api, openExternal } from './api.js';
import { cellId, latToY, lonToX } from './grid.js';
import { el, formatDate, toast } from './ui.js';

const STATES = { operating: '运营', construction: '在建', proposed: '规划', disused: '停用' };
const KINDS = { rail: '铁路', narrow_gauge: '窄轨铁路', subway: '地铁', light_rail: '轻轨', tram: '有轨电车' };
const USAGES = { main: '干线', branch: '支线', industrial: '工业专用线', tourism: '旅游线', military: '军用线', test: '试验线' };
const SERVICES = { siding: '站线', crossover: '渡线', spur: '岔线', yard: '调车场线' };
const SHOWN = new Set(['name', 'ref', 'usage', 'service', 'maxspeed', 'maxspeed:forward', 'maxspeed:backward',
  'railway:preferred_direction', 'electrified', 'voltage', 'frequency', 'gauge', 'highspeed', 'railway:track_ref',
  'operator', 'start_date', 'railway']);

function row(label, value) {
  if (value === undefined || value === null || value === '') return null;
  return el('tr', {}, el('th', {}, label), el('td', {}, String(value)));
}

// A line is often several OSM ways where it is clicked: one per track of a
// double-track line, or split at a bridge or a speed change.  Ways that are
// the same line (name, number, kind, speed) are one entry; the details say
// how many ways there are and which tags differ, and can show each of them.
function lineKey(line) {
  const t = line.tags || {};
  return [line.state, line.kind, t.name || '', t.ref || '', line.service || '', line.speedLabel || ''].join('|');
}

function groupLines(lines) {
  const groups = new Map();
  for (const line of lines) {
    const key = lineKey(line);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(line);
  }
  return [...groups.values()];
}

function segments(group, index, onSwitch) {
  const keys = [...new Set(group.flatMap((l) => Object.keys(l.tags || {})))].sort();
  const differing = keys.filter((k) => new Set(group.map((l) => (l.tags || {})[k] ?? '')).size > 1);
  return el('div', { class: 'details-segments' },
    el('div', {}, `此处同一线路有 ${group.length} 段 OSM 线路（常见于上下行两条股道，或在桥梁、限速变化处分段）`
      + (differing.length ? '，不同的标签：' : '，标签相同。')),
    differing.length ? el('table', {}, differing.map((k) => el('tr', {}, el('th', {}, k),
      el('td', {}, group.map((l) => (l.tags || {})[k] ?? '（无）').join(' / '))))) : null,
    el('div', { class: 'segment-buttons' }, group.map((l, i) => el('button', {
      type: 'button', class: i === index ? 'link current' : 'link', disabled: i === index,
      onclick: () => onSwitch(i),
    }, `第 ${i + 1} 段`))),
  );
}

function describe(line, railAt, group = [line], index = 0, onSwitch = null) {
  const t = line.tags;
  const type = [STATES[line.state] || '', KINDS[line.kind] || line.kind || ''].join('');
  const speed = line.speedLabel
    ? `${line.speedLabel}${line.colorSpeed != null ? `（着色按 ${line.colorSpeed} km/h）` : '（无着色速度）'}`
    : '无速度信息';
  const others = Object.keys(t).filter((k) => k.startsWith('railway:') && !SHOWN.has(k)).sort();
  const all = Object.keys(t).sort().map((k) => el('tr', {}, el('th', {}, k), el('td', {}, t[k])));
  return el('div', { class: 'details' },
    el('div', { class: 'details-title' }, t.name || t.ref || `线路 ${line.id}`),
    el('table', {}, [
      row('名称', t.name), row('线路编号', t.ref), row('类型', type),
      row('用途', USAGES[line.usage] || line.usage), row('配线类型', SERVICES[line.service] || line.service),
      row('速度', speed),
      row('maxspeed', t.maxspeed), row('maxspeed:forward', t['maxspeed:forward']),
      row('maxspeed:backward', t['maxspeed:backward']), row('优先方向', t['railway:preferred_direction']),
      row('电气化', t.electrified), row('电压', t.voltage), row('频率', t.frequency),
      row('轨距', t.gauge), row('高速', t.highspeed), row('股道号', t['railway:track_ref']),
      row('运营方', t.operator), row('开通日期', t.start_date),
      ...others.map((k) => row(k, t[k])),
      railAt ? row('数据下载日期', formatDate(railAt)) : row('数据来源', '联网查询（未缓存区域）'),
    ].filter(Boolean)),
    group.length > 1 && onSwitch ? segments(group, index, onSwitch) : null,
    el('details', {}, el('summary', {}, `全部标签（${Object.keys(t).length}）`), el('table', { class: 'all-tags' }, all)),
    el('button', { type: 'button', class: 'link', onclick: () => openExternal(`https://www.openstreetmap.org/way/${line.id}`) },
      '在 openstreetmap.org 查看'),
  );
}

// One line of the chooser: what the line is, then what tells it apart from its neighbours.
function lineLabel(line, withTrack = true) {
  const t = line.tags || {};
  const type = [STATES[line.state] || '', KINDS[line.kind] || ''].join('');
  const name = t.name || t.ref || type || `线路 ${line.id}`;
  const extra = [
    t.name && t.ref ? t.ref : null,
    SERVICES[line.service] || null,
    withTrack && t['railway:track_ref'] ? `${t['railway:track_ref']} 道` : null,
    line.speedLabel ? `${line.speedLabel} km/h` : '无速度信息',
  ].filter(Boolean);
  return `${name} · ${extra.join(' · ')}`;
}

function chooser(groups, pick) {
  return el('div', { class: 'details' },
    el('div', { class: 'details-title' }, `此处有 ${groups.length} 条线路`),
    ...groups.map((group) => el('button', { type: 'button', class: 'result', onclick: () => pick(group) },
      el('span', { class: 'result-name' },
        group.length > 1 ? `${lineLabel(group[0], false)}（${group.length} 段）` : lineLabel(group[0])))));
}

export function setupDetails(map, cells) {
  let popup = null;
  let pending = 0;
  const show = (lngLat, node) => {
    if (popup) popup.remove();
    popup = new maplibregl.Popup({ maxWidth: '380px', className: 'details-popup' }).setLngLat(lngLat).setDOMContent(node).addTo(map);
  };

  document.addEventListener('keydown', (ev) => {
    if (ev.key === 'Escape' && popup) { popup.remove(); popup = null; }
  });

  // One line (a group of ways, see lineKey): its details, with a switch between its ways.
  const showGroup = (lngLat, group, railAt, index = 0) => show(lngLat,
    describe(group[index], railAt, group, index, (i) => showGroup(lngLat, group, railAt, i)));
  const showLines = (lngLat, lines, railAt) => {
    const groups = groupLines(lines);
    if (groups.length === 1) showGroup(lngLat, groups[0], railAt);
    else show(lngLat, chooser(groups, (group) => showGroup(lngLat, group, railAt)));
  };

  map.on('click', async (e) => {
    if (cells.mode !== 'browse') return;
    const x = Math.floor(lonToX(e.lngLat.lng));
    const y = Math.floor(latToY(e.lngLat.lat));
    const cid = cellId(x, y);
    const request = ++pending;
    const pad = 5;
    const features = map.queryRenderedFeatures(
      [[e.point.x - pad, e.point.y - pad], [e.point.x + pad, e.point.y + pad]],
      { layers: ['rail-lines', 'rail-proposed'] },
    );
    const seen = new Map();
    for (const f of features) if (f.id !== undefined && !seen.has(f.id)) seen.set(f.id, f);
    try {
      if (seen.size && cells.isCached(cid)) {
        const load = async (id) => {
          const info = await api.get(`/api/details?cell=${cid}&way=${id}`);
          if (request === pending) show(e.lngLat, describe(info, info.railAt));
        };
        if (seen.size === 1) await load([...seen.keys()][0]);
        else {
          // Local and fast: fetch every candidate so the list can name them.
          const infos = (await Promise.all([...seen.keys()].slice(0, 20)
            .map((id) => api.get(`/api/details?cell=${cid}&way=${id}`).catch(() => null)))).filter(Boolean);
          if (request !== pending || !infos.length) return;
          showLines(e.lngLat, infos, infos[0].railAt);
        }
        return;
      }
      if (cells.isCached(cid)) return; // clicked an empty spot of a cached cell
      if (document.querySelector('#net-status').dataset.state === 'offline') {
        toast('此区域未缓存，离线时无法查看详情');
        return;
      }
      // Uncached area: ask Overpass for lines near the click (SPEC §6.8, P2).
      const metersPerPixel = (40075016.686 * Math.cos((e.lngLat.lat * Math.PI) / 180)) / (512 * 2 ** map.getZoom());
      const radius = Math.min(50, Math.max(5, metersPerPixel * 10));
      const result = await api.get(`/api/details/online?lon=${e.lngLat.lng}&lat=${e.lngLat.lat}&radius=${radius.toFixed(0)}`);
      if (request !== pending || !result.lines.length) return;
      showLines(e.lngLat, result.lines, null);
    } catch (err) {
      if (request === pending) toast(err.message, 'error');
    }
  });
}
