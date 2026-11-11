import { Markdown, matchesKey, truncateToWidth } from '@earendil-works/pi-tui';
import { getMarkdownTheme } from '@earendil-works/pi-coding-agent';
import { clean } from './tool-render.mjs';
import { subagentStatus } from './product-ui.mjs';

/** Native child list; Enter shows the original summary, Esc returns or closes. No effects. */
export function agentsPanel(children, policy, finish, requestRender, rows = () => process.stdout.rows ?? 30) {
  const items = [...children].reverse();
  let selected = 0, detail = null, offset = 0;
  const pageSize = () => Math.max(1, Math.floor((rows() - 8) / 2));
  return {
    focused: false,
    invalidate() {},
    render(width) {
      const clip = text => truncateToWidth(text, width, '…');
      if (detail) {
        const child = items[selected];
        const body = new Markdown(clean(child.summary || '（没有报告文本）'), 0, 0, getMarkdownTheme()).render(Math.max(10, width - 2));
        const view = Math.max(3, rows() - 7);
        offset = Math.max(0, Math.min(offset, body.length - view));
        return [clip(`子代理报告 · ${subagentStatus(child)}`), clip('  任务：' + clean(child.task).replace(/\s+/g, ' ')), '',
          ...body.slice(offset, offset + view).map(line => '  ' + line), '',
          clip(`↑↓ 滚动 · Esc 返回列表${body.length > view ? ` · ${offset + 1}-${Math.min(body.length, offset + view)}/${body.length}` : ''}`)];
      }
      const lines = [clip(`子代理 · ${items.length} 个 · ${policy}`), ''];
      if (!items.length) lines.push('  本会话还没有子代理。', '  可以直接说：启动两个子代理，分别梳理开发流程和验证流程，再汇总。');
      const start = Math.floor(selected / pageSize()) * pageSize();
      for (const [index, child] of items.slice(start, start + pageSize()).entries()) {
        lines.push(clip(`${start + index === selected ? '❯' : ' '} ${subagentStatus(child)}`));
        lines.push(clip('    ' + clean(child.task).replace(/\s+/g, ' ')));
      }
      lines.push('', clip(items.length ? '↑↓ 选择 · Enter 查看报告 · Esc 关闭' : 'Esc 关闭'));
      return lines;
    },
    handleInput(data) {
      if (detail) {
        if (matchesKey(data, 'escape') || matchesKey(data, 'left')) { detail = null; offset = 0; }
        else if (matchesKey(data, 'up')) offset = Math.max(0, offset - 1);
        else if (matchesKey(data, 'down')) offset += 1;
        else if (matchesKey(data, 'pageUp')) offset = Math.max(0, offset - 10);
        else if (matchesKey(data, 'pageDown')) offset += 10;
      } else if (matchesKey(data, 'escape') || matchesKey(data, 'ctrl+c')) return finish(null);
      else if (matchesKey(data, 'up')) selected = Math.max(0, selected - 1);
      else if (matchesKey(data, 'down')) selected = Math.min(Math.max(0, items.length - 1), selected + 1);
      else if (matchesKey(data, 'enter') && items[selected]) { detail = items[selected]; offset = 0; }
      requestRender();
    },
  };
}
