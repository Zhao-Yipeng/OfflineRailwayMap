// Small UI helpers: modal dialogs, toasts, attribution, scale bar.
import { openExternal } from './api.js';

export const $ = (id) => document.getElementById(id);

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
    ' | Basemap: ', externalLink('Protomaps', 'https://protomaps.com/'),
  );
}

/** Connectivity line in the status bar (SPEC §6.11, §13). */
export function renderStatus(status) {
  const node = $('net-status');
  const problems = Object.values(status.services || {}).filter((s) => s.state !== 'ok');
  if (status.network === 'offline') {
    node.dataset.state = 'offline';
    node.textContent = '离线';
    node.title = '无法连接网络；已缓存的区域仍可正常使用。';
  } else if (problems.length) {
    node.dataset.state = 'degraded';
    node.textContent = `在线 · ${problems.map((p) => p.message).join(' · ')}`;
    node.title = problems.map((p) => p.message).join('\n');
  } else {
    node.dataset.state = 'online';
    node.textContent = '在线';
    node.title = '';
  }
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
