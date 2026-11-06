// After the overlay 「永远显示在建线路」 downloaded China's extracts (SPEC §7.9): ask
// whether the extracts downloaded only for it are deleted now or kept 7 days.
// Extracts shared with cell downloads are not part of the question.
import { api } from './api.js';
import { dialog, formatBytes, toast } from './ui.js';

let asking = false;

export async function askAboutExtracts(question) {
  if (!question || asking) return;
  asking = true;
  try {
    const answer = await dialog({
      title: '在建线路已准备好',
      message: `为了显示全国的在建线路，专门下载了 ${question.count} 个省级地区的数据包（现占 ${formatBytes(question.bytes)}），`
        + '在建线路已经提取出来，删除这些数据包不影响显示。\n\n'
        + '要立即删除吗？保留的话，7 天内下载这些地区的方格可以直接用，不用重新下载，7 天后自动删除。\n\n'
        + '下载方格时已有或正在下载的数据包是共用的，不在此列。',
      buttons: [
        { text: '保留 7 天', value: 'keep' },
        { text: '立即删除', value: 'delete', kind: 'primary' },
      ],
    });
    if (answer === null) return; // closed: asked again at the next start
    const result = await api.post('/api/construction/decide',
      { delete: answer === 'delete', regions: question.regions });
    if (answer === 'keep') toast('这些数据包保留 7 天');
    else if (result.deleted) {
      toast(`已删除 ${result.deleted} 个数据包，释放 ${formatBytes(result.bytes)}`
        + (result.kept ? `；${result.kept} 个方格正要用到，已保留` : ''));
    } else toast('这些数据包方格正要用到，已保留');
  } catch (err) {
    toast(`操作失败：${err.message}`, 'error');
  } finally {
    asking = false;
  }
}
