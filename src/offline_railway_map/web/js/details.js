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

function describe(line, railAt) {
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
    el('details', {}, el('summary', {}, `全部标签（${Object.keys(t).length}）`), el('table', { class: 'all-tags' }, all)),
    el('button', { type: 'button', class: 'link', onclick: () => openExternal(`https://www.openstreetmap.org/way/${line.id}`) },
      '在 openstreetmap.org 查看'),
  );
}

function chooser(lines, pick) {
  return el('div', { class: 'details' },
    el('div', { class: 'details-title' }, `此处有 ${lines.length} 条线路`),
    ...lines.map((line) => el('button', { type: 'button', class: 'result', onclick: () => pick(line) },
      el('span', { class: 'result-name' }, line.label || `线路 ${line.id}`))));
}

export function setupDetails(map, cells) {
  let popup = null;
  let pending = 0;
  const show = (lngLat, node) => {
    if (popup) popup.remove();
    popup = new maplibregl.Popup({ maxWidth: '380px', className: 'details-popup' }).setLngLat(lngLat).setDOMContent(node).addTo(map);
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
          const lines = [...seen.values()].map((f) => ({ id: f.id, label: f.properties.lb ? `速度 ${f.properties.lb}` : `线路 ${f.id}` }));
          show(e.lngLat, chooser(lines, (line) => load(line.id)));
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
      if (result.lines.length === 1) show(e.lngLat, describe(result.lines[0], null));
      else show(e.lngLat, chooser(result.lines.map((l) => ({ id: l.id, label: l.tags.name || l.speedLabel || `线路 ${l.id}`, line: l })),
        (pick) => show(e.lngLat, describe(result.lines.find((l) => l.id === pick.id), null))));
    } catch (err) {
      if (request === pending) toast(err.message, 'error');
    }
  });
}
