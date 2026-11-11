import { Markdown, matchesKey, truncateToWidth } from '@earendil-works/pi-tui';
import { getMarkdownTheme } from '@earendil-works/pi-coding-agent';
import { clean } from './tool-render.mjs';
import { subagentStatus } from './product-ui.mjs';

/** Built-in types; host-supplied definitions (state.subagent_types) take precedence. */
export const BUILTIN_AGENTS = [
  { name: 'kernel-scout', source: '内置', description: '查找文件、搜索代码、追踪实现', tools: '只读' },
  { name: 'kernel-reviewer', source: '内置', description: '对改动或结论做独立复核', tools: '只读' },
];

/**
 * Native agents view in the style of other coding CLIs: available agent types
 * first, then this session's runs. Enter opens details; Esc returns or closes.
 */
export function agentsPanel(children, types, finish, requestRender, rows = () => process.stdout.rows ?? 30) {
  const runs = [...children].reverse();
  const entries = [...types.map(type => ({ kind: 'type', type })), ...runs.map(child => ({ kind: 'run', child }))];
  let selected = 0, detail = null, offset = 0;
  const groups = () => {
    const lines = [];
    let source;
    for (const [index, entry] of entries.entries()) {
      if (entry.kind === 'type' && entry.type.source !== source) {
        source = entry.type.source;
        lines.push({ text: `  ${source}` });
      }
      if (entry.kind === 'run' && (index === 0 || entries[index - 1].kind !== 'run')) lines.push({ text: '' }, { text: `  本会话（${runs.length}）` });
      lines.push({ index, entry });
    }
    return lines;
  };
  return {
    focused: false,
    invalidate() {},
    render(width) {
      const clip = text => truncateToWidth(text, width, '…');
      if (detail) {
        const view = Math.max(3, rows() - 6);
        let head, body;
        if (detail.kind === 'type') {
          head = [clip(detail.type.name), clip(`  ${detail.type.source} · ${detail.type.tools}`)];
          body = new Markdown(clean(detail.type.prompt || detail.type.description), 0, 0, getMarkdownTheme()).render(Math.max(10, width - 2));
        } else {
          head = [clip(subagentStatus(detail.child)), clip('  任务：' + clean(detail.child.task).replace(/\s+/g, ' '))];
          body = new Markdown(clean(detail.child.summary || '（尚无报告）'), 0, 0, getMarkdownTheme()).render(Math.max(10, width - 2));
        }
        offset = Math.max(0, Math.min(offset, body.length - view));
        return [...head, '', ...body.slice(offset, offset + view).map(line => '  ' + line), '',
          clip(`Esc 返回${body.length > view ? ' · ↑↓ 滚动' : ''}`)];
      }
      const lines = [clip('子代理'), ''];
      for (const item of groups()) {
        if (!('entry' in item)) { lines.push(clip(item.text)); continue; }
        const mark = item.index === selected ? '❯' : ' ';
        if (item.entry.kind === 'type') {
          lines.push(clip(`${mark} ${item.entry.type.name.padEnd(18)} ${item.entry.type.description}`));
        } else lines.push(clip(`${mark} ${subagentStatus(item.entry.child)}`));
      }
      const view = Math.max(4, rows() - 4);
      const cursor = lines.findIndex(line => line.startsWith('❯'));
      const start = Math.max(0, Math.min(cursor - Math.floor(view / 2), lines.length - view));
      return [...lines.slice(start, start + view), '', clip('↑↓ 选择 · Enter 查看 · Esc 关闭')];
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
      else if (matchesKey(data, 'down')) selected = Math.min(Math.max(0, entries.length - 1), selected + 1);
      else if (matchesKey(data, 'enter') && entries[selected]) { detail = entries[selected]; offset = 0; }
      requestRender();
    },
  };
}
