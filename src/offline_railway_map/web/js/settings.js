// Settings dialog with the About section (SPEC §6.10, §6.11).
import { api } from './api.js';
import { $, dialog, el, toast } from './ui.js';

export function setupSettings(onSaved) {
  $('btn-settings').addEventListener('click', async () => {
    let data;
    try {
      data = await api.get('/api/settings');
    } catch (err) {
      toast(`无法读取设置：${err.message}`, 'error');
      return;
    }
    const s = data.settings;
    const zoom = el('select', { id: 'set-zoom' }, [12, 13, 14, 15].map((z) =>
      el('option', { value: String(z), selected: z === s.basemapMaxzoom }, `z${z}`)));
    const endpoints = el('textarea', { id: 'set-overpass', rows: 4 });
    endpoints.value = s.overpassEndpoints.join('\n');
    const auto = s.basemapSource === 'auto';
    const sourceAuto = el('input', { type: 'radio', name: 'source', value: 'auto', checked: auto });
    const sourceCustom = el('input', { type: 'radio', name: 'source', value: 'custom', checked: !auto });
    const sourceUrl = el('input', { type: 'url', id: 'set-source', placeholder: 'https://…/basemap.pmtiles' });
    sourceUrl.value = auto ? '' : s.basemapSource;
    const limit = el('input', { type: 'number', id: 'set-limit', min: 256, max: 20480, step: 256 });
    limit.value = String(s.onlineCacheLimitMb);
    const outline = el('input', { type: 'checkbox', id: 'set-outline', checked: s.showCachedOutline });
    const construction = el('input', { type: 'checkbox', id: 'set-construction', checked: s.alwaysShowConstruction });
    const about = data.about;
    const body = el('div', { class: 'settings' },
      el('label', {}, '离线底图最大级别', zoom, el('small', {}, '只影响之后下载或更新底图的方格')),
      el('label', {}, 'Overpass 服务器（每行一个，按顺序使用）', endpoints),
      el('fieldset', {}, el('legend', {}, '离线底图来源'),
        el('label', { class: 'inline' }, sourceAuto, '自动（Protomaps 最新的 v4 每日构建）'),
        el('label', { class: 'inline' }, sourceCustom, '自定义 PMTiles 地址'), sourceUrl),
      el('label', {}, '在线瓦片硬盘缓存上限（MB）', limit),
      el('label', { class: 'inline' }, outline, '浏览模式下显示已缓存方格的边框'),
      el('div', { class: 'option' },
        el('label', { class: 'inline' }, construction, '永远显示在建线路'),
        el('small', {}, '未缓存的区域也在所有缩放级别显示在建线路，样式与已建成线路相同（数据按需从 Overpass 获取）')),
      el('details', { class: 'about' }, el('summary', {}, `关于 ${about.app} ${about.version}`),
        el('p', {}, `许可证：${about.license}`),
        el('p', {}, about.disclaimer),
        el('table', {}, about.components.map(([name, license]) => el('tr', {}, el('td', {}, name), el('td', {}, license))))),
    );
    for (;;) {
      const action = await dialog({
        title: '设置', body,
        buttons: [{ text: '取消', value: null }, { text: '保存', value: 'save', kind: 'primary' }],
      });
      if (action !== 'save') return;
      const payload = {
        basemapMaxzoom: Number(zoom.value),
        overpassEndpoints: endpoints.value.split('\n').map((v) => v.trim()).filter(Boolean),
        basemapSource: sourceCustom.checked ? sourceUrl.value.trim() : 'auto',
        onlineCacheLimitMb: Number(limit.value),
        showCachedOutline: outline.checked,
        alwaysShowConstruction: construction.checked,
      };
      try {
        const saved = await api.post('/api/settings', payload);
        toast('设置已保存');
        onSaved(saved.settings);
        return;
      } catch (err) {
        toast(`设置无效：${err.message}`, 'error');
      }
    }
  });
}
