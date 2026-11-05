// 「清空缓存」(SPEC §6.6): always available, asks first, then deletes data\.
import { api } from './api.js';
import { $, confirmDialog, dialog, el, formatBytes, toast } from './ui.js';

export function setupClearButton(cells) {
  const button = $('btn-clear');
  button.disabled = false;
  button.addEventListener('click', async () => {
    let count = 0, bytes = 0;
    for (const row of cells.cells.values()) {
      count += 1;
      bytes += row.bytes || 0;
    }
    const ok = await confirmDialog({
      title: '清空缓存',
      message: `将删除本程序下载和生成的全部数据（${count} 个方格，约 ${formatBytes(bytes)}）以及所有设置，程序文件夹将恢复到刚解压时的状态。确定吗？`,
      okText: '全部删除',
      danger: true,
    });
    if (!ok) return;
    button.disabled = true;
    try {
      const result = await api.post('/api/clear');
      cells.discardEdits();
      cells.setMode('edit');
      if (result.ok) {
        toast('缓存已清空');
      } else {
        await dialog({
          title: '有文件未能删除',
          message: '以下文件正被其他程序占用，程序下次启动时会再次尝试删除：',
          body: el('ul', { class: 'failed-files' }, result.failed.map((f) => el('li', {}, f))),
          buttons: [{ text: '知道了', value: true, kind: 'primary' }],
        });
      }
    } catch (err) {
      toast(`清空失败：${err.message}`, 'error');
    } finally {
      button.disabled = false;
    }
  });
}
