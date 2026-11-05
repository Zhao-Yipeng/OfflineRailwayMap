// Small UI helpers: modal dialogs, toasts, attribution, scale bar.
import { openExternal } from './api.js';

export const $ = (id) => document.getElementById(id);

export function formatBytes(bytes) {
  if (!bytes) return '0 MB';
  if (bytes >= 1e9) return `${(bytes / 1e9).toFixed(2)} GB`;
  if (bytes < 1e5) return `${Math.max(1, Math.round(bytes / 1e3))} KB`;
  return `${(bytes / 1e6).toFixed(1)} MB`;
}

export function formatDate(seconds) {
  const d = new Date(seconds * 1000);
  const pad = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = value;
    else if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? '' : value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

/** Link that opens in the system browser. */
export function externalLink(text, href) {
  return el('a', {
    href,
    onclick: (ev) => { ev.preventDefault(); openExternal(href); },
  }, text);
}

/**
 * Modal dialog. Resolves to the value of the clicked button.
 * buttons: [{ text, value, kind: 'primary' | 'danger' | undefined }]
 */
export function dialog({ title, message, body, buttons }) {
  return new Promise((resolve) => {
    const root = $('modal-root');
    const close = (value) => { root.replaceChildren(); document.removeEventListener('keydown', onKey); resolve(value); };
    const onKey = (ev) => { if (ev.key === 'Escape') close(null); };
    const actions = buttons.map((b) => el('button', { type: 'button', class: b.kind || '', onclick: () => close(b.value) }, b.text));
    const box = el('div', { class: 'modal panel', role: 'dialog', 'aria-modal': 'true' },
      title ? el('h2', { text: title }) : null,
      message ? el('p', { text: message }) : null,
      body || null,
      el('div', { class: 'actions' }, actions));
    root.replaceChildren(el('div', { class: 'modal-backdrop' }, box));
    document.addEventListener('keydown', onKey);
    actions[actions.length - 1].focus();
  });
}

export async function confirmDialog({ title, message, okText = '确定', cancelText = '取消', danger = false }) {
  const value = await dialog({
    title, message,
    buttons: [
      { text: cancelText, value: false },
      { text: okText, value: true, kind: danger ? 'danger' : 'primary' },
    ],
  });
  return value === true;
}

export function toast(message, kind = 'info', ms = 4000) {
  const node = el('div', { class: `toast panel ${kind}` }, message);
  $('toast-root').append(node);
  setTimeout(() => node.remove(), ms);
}

export function showFatal(message) {
  document.body.append(el('div', { class: 'fatal' }, message));
}

/** Attribution required by the data sources (SPEC §12.7). */
export function renderAttribution() {
  $('attribution').replaceChildren(
    'Data ', externalLink('© OpenStreetMap contributors', 'https://www.openstreetmap.org/copyright'),
    ' | Style: ', externalLink('CC-BY-SA 2.0', 'https://creativecommons.org/licenses/by-sa/2.0/'), ' ',
    externalLink('OpenRailwayMap', 'https://www.openrailwaymap.org/'), ' and OpenStreetMap',
    ' | Basemap: ', externalLink('OpenFreeMap', 'https://openfreemap.org/'), ' ',
    externalLink('© OpenMapTiles', 'https://www.openmaptiles.org/'), ', ',
    externalLink('Protomaps', 'https://protomaps.com/'),
  );
}

/** Keep the download panel just above the attribution, however many lines the attribution takes. */
export function trackBottomRow() {
  const row = document.querySelector('.attribution-row');
  const update = () => document.documentElement.style.setProperty('--bottom-row-height', `${row.offsetHeight}px`);
  new ResizeObserver(update).observe(row);
  update();
}

/**
 * Connectivity in the status bar (SPEC §6.11, §13): 「在线」/「离线」 next to the
 * scale, and below them one line per service problem or background task
 * (e.g. 「正在加载在建线路」), so a long message is never cut off.
 */
export function renderStatus(status) {
  const node = $('net-status');
  const notesNode = $('status-notes');
  const problems = Object.values(status.services || {}).filter((s) => s.state !== 'ok').map((p) => p.message);
  const notes = Object.values(status.notes || {});
  let lines = [];
  if (status.network === 'offline') {
    node.dataset.state = 'offline';
    node.textContent = '离线';
    node.title = '无法连接网络；已缓存的区域仍可正常使用。';
  } else {
    node.dataset.state = problems.length ? 'degraded' : 'online';
    node.textContent = '在线';
    node.title = problems.join('\n');
    lines = [
      ...problems.map((text) => el('div', { class: 'status-problem' }, text)),
      ...notes.map((text) => el('div', {}, text)),
    ];
  }
  notesNode.replaceChildren(...lines);
  notesNode.hidden = !lines.length;
}

/** Metric scale bar that follows the map (map units: 512 px tiles). */
export function attachScale(map) {
  const bar = document.querySelector('#scale .scale-bar');
  const label = document.querySelector('#scale .scale-label');
  const maxWidth = 100;
  const update = () => {
    const { lat } = map.getCenter();
    const metersPerPixel = (40075016.686 * Math.cos((lat * Math.PI) / 180)) / (512 * 2 ** map.getZoom());
    const maxMeters = metersPerPixel * maxWidth;
    const exponent = 10 ** Math.floor(Math.log10(maxMeters));
    const nice = [5, 2, 1].map((m) => m * exponent).find((d) => d <= maxMeters) || exponent;
    bar.style.width = `${Math.round(nice / metersPerPixel)}px`;
    label.textContent = nice >= 1000 ? `${nice / 1000} km` : `${nice} m`;
  };
  map.on('move', update);
  update();
}
