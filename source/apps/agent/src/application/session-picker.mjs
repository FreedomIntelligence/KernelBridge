import { TuiMainScreen, ProcessTerminal, Input, matchesKey, truncateToWidth, visibleWidth, replaceControlCharacters } from '@earendil-works/pi-tui';
import { basename } from 'node:path';

export function sessionActivity(item) {
  if (!item.updated) return '时间未知';
  const minutes = Math.max(0, Math.floor((Date.now() - item.updated) / 60000));
  if (minutes < 1) return '刚刚';
  if (minutes < 60) return `${minutes} 分钟前`;
  if (minutes < 1440) return `${Math.floor(minutes / 60)} 小时前`;
  if (minutes < 10080) return `${Math.floor(minutes / 1440)} 天前`;
  return new Date(item.updated).toLocaleDateString('zh-CN');
}

const sizeLabel = bytes => bytes == null ? '大小未知' : bytes < 1024 ? `${bytes} B`
  : bytes < 1048576 ? `${(bytes / 1024).toFixed(1)} KB` : `${(bytes / 1048576).toFixed(1)} MB`;
const clean = value => replaceControlCharacters(String(value ?? '').replace(/\s+/g, ' '));

export function filterSessions(sessions, query) {
  const terms = query.normalize('NFKC').trim().toLocaleLowerCase().split(/\s+/).filter(Boolean);
  return sessions.filter(item => {
    const text = [item.title, item.application_session_id, item.cwd, item.project?.branch].filter(Boolean).join(' ').normalize('NFKC').toLocaleLowerCase();
    return terms.every(term => text.includes(term));
  });
}

/** One searchable native component shared by the CLI and the running Pi TUI. */
export function sessionPicker(sessions, finish, requestRender, options = {}) {
  let all = Boolean(options.all), worktrees = false, branchOnly = false, selected = 0;
  const input = new Input({ prompt: '搜索 › ', placeholder: '标题、项目、分支或会话 ID…' });
  const pageSize = () => Math.max(1, Math.min(8, Math.floor(((options.rows?.() ?? process.stdout.rows ?? 30) - 11) / 3)));
  const rows = () => filterSessions(sessions, input.getValue()).filter(item => {
    const current = item.application_session_id === options.currentId;
    const sameProject = !options.cwd || item.cwd === options.cwd;
    const sameRepo = options.project?.common_directory && item.project?.common_directory === options.project.common_directory;
    const branch = current ? options.project?.branch : item.project?.branch;
    return (all || sameProject || (worktrees && sameRepo)) && (!branchOnly || Boolean(options.project?.branch && branch === options.project.branch));
  });
  return {
    get focused() { return input.focused; },
    set focused(value) { input.focused = value; },
    render(width) {
      const found = rows(); selected = Math.max(0, Math.min(selected, found.length - 1));
      const clip = text => truncateToWidth(text, width, '…');
      const inner = Math.max(1, width - 4);
      const box = input.render(inner).map(line => `│ ${line}${' '.repeat(Math.max(0, inner - visibleWidth(line)))} │`);
      const lines = [clip(`KernelBridge · 恢复会话 (${found.length ? selected + 1 : 0}/${found.length})`),
        '╭' + '─'.repeat(Math.max(0, width - 2)) + '╮', ...box, '╰' + '─'.repeat(Math.max(0, width - 2)) + '╯',
        clip('  ' + (all ? '全部项目' : worktrees ? '同仓库 worktree' : basename(options.cwd ?? '') || '项目会话')), ''];
      const start = Math.floor(selected / pageSize()) * pageSize();
      if (!found.length) lines.push('  没有匹配的会话', '');
      for (const [offset, item] of found.slice(start, start + pageSize()).entries()) {
        const current = item.application_session_id === options.currentId;
        const branch = current ? options.project?.branch : item.project?.branch;
        const title = item.has_history === false ? '新会话' : item.title || '未命名会话';
        lines.push(clip(`${start + offset === selected ? '❯' : ' '} ${clean(title)}${current ? '  [当前]' : ''}`));
        lines.push(clip(`  ${sessionActivity(item)} · ${clean(branch) || '分支未知'} · 日志 ${sizeLabel(item.bytes)}${all || worktrees ? ' · ' + clean(item.cwd) : ''}`));
        lines.push('');
      }
      if (start + pageSize() < found.length) lines.push(clip(`↓ 还有 ${found.length - start - pageSize()} 个会话`));
      lines.push(clip(`Ctrl+A 全项目${all ? ' ✓' : ''} · Ctrl+B 当前分支${branchOnly ? ' ✓' : ''} · Ctrl+W worktree${worktrees ? ' ✓' : ''}`),
        clip('↑↓ 选择 · PgUp/PgDn 翻页 · Enter 恢复 · Esc 取消'));
      return lines.map(clip);
    },
    invalidate() { input.invalidate(); },
    handleInput(data) {
      if (matchesKey(data, 'escape') || matchesKey(data, 'ctrl+c') || matchesKey(data, 'ctrl+d')) return finish(null);
      if (matchesKey(data, 'ctrl+a')) { all = !all; selected = 0; }
      else if (matchesKey(data, 'ctrl+b')) { branchOnly = !branchOnly; selected = 0; }
      else if (matchesKey(data, 'ctrl+w')) { worktrees = !worktrees; all = false; selected = 0; }
      else if (matchesKey(data, 'enter')) { const item = rows()[selected]; if (item) return finish(item.application_session_id); }
      else if (matchesKey(data, 'up')) selected = Math.max(0, selected - 1);
      else if (matchesKey(data, 'down')) selected = Math.min(Math.max(0, rows().length - 1), selected + 1);
      else if (matchesKey(data, 'pageUp')) selected = Math.max(0, selected - pageSize());
      else if (matchesKey(data, 'pageDown')) selected = Math.min(Math.max(0, rows().length - 1), selected + pageSize());
      else { input.handleInput(data); selected = 0; }
      requestRender();
    },
  };
}

/** Native Pi widgets, no model calls and no host lease until a session is chosen. */
export async function pickSession(sessions, options = {}) {
  if (!process.stdin.isTTY) throw new Error('Use an explicit session ID or --last outside a terminal');
  if (!sessions.length) throw new Error('没有可恢复的会话。用 --all 查看其他项目。');
  const tui = new TuiMainScreen(new ProcessTerminal());
  let finish;
  const result = new Promise(resolve => { finish = resolve; });
  const component = sessionPicker(sessions, finish, () => tui.requestRender(), options);
  tui.addChild(component); tui.setFocus(component);
  try { tui.start(); return await result; }
  finally { tui.stop(); }
}
