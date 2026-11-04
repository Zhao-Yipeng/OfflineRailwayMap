// Legend (SPEC §6.9): speed colours as on the website, line styles, label format.
import { OVER_360, SPEED_BANDS, UNKNOWN } from './rail.js';
import { $, el } from './ui.js';

function swatch(color, dashed = false) {
  return el('span', { class: `swatch${dashed ? ' dashed' : ''}`, style: `--c:${color}` });
}

export function setupLegend() {
  const panel = $('legend');
  const button = $('btn-legend');
  const bands = [];
  let low = 0;
  for (const [upper, color] of SPEED_BANDS) {
    bands.push(el('div', { class: 'legend-row' }, swatch(color), `${low === 0 ? '≤' : `${low}–`}${upper} km/h`));
    low = upper + 1;
  }
  bands.push(el('div', { class: 'legend-row' }, swatch(OVER_360), '> 360 km/h'));
  bands.push(el('div', { class: 'legend-row' }, swatch(UNKNOWN), '无速度信息'));
  panel.replaceChildren(
    el('div', { class: 'legend-title' }, '最高速度'),
    el('div', { class: 'legend-grid' }, bands),
    el('div', { class: 'legend-title' }, '线型'),
    el('div', { class: 'legend-row' }, swatch('#00CB47'), '运营 / 在建（实线）'),
    el('div', { class: 'legend-row' }, swatch('#00CB47', true), '规划（虚线）'),
    el('div', { class: 'legend-title' }, '速度数字'),
    el('div', { class: 'legend-note' }, '120：限速 120 km/h'),
    el('div', { class: 'legend-note' }, '120/80：正向 120、反向 80'),
    el('div', { class: 'legend-note' }, '120 (80)：主方向 120，括号内为另一方向'),
    el('div', { class: 'legend-note' }, '数字从 z9 起显示；缩小时只按颜色区分'),
  );
  button.addEventListener('click', () => {
    panel.hidden = !panel.hidden;
    button.setAttribute('aria-pressed', String(!panel.hidden));
  });
}
