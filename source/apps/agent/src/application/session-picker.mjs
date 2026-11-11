import { TuiMainScreen, ProcessTerminal, SelectList, Input, matchesKey } from '@earendil-works/pi-tui';

export function sessionActivity(item) {
  return item.updated ? new Date(item.updated).toLocaleString('zh-CN', {
    month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false,
  }) : '时间未知';
}

export function filterSessions(sessions, query) {
  const terms = query.normalize('NFKC').trim().toLocaleLowerCase().split(/\s+/).filter(Boolean);
  return sessions.filter(item => {
    const text = [item.title, item.application_session_id, item.cwd].filter(Boolean).join(' ').normalize('NFKC').toLocaleLowerCase();
    return terms.every(term => text.includes(term));
  });
}

/** One searchable native component shared by the CLI and the running Pi TUI. */
export function sessionPicker(sessions, finish, requestRender) {
  const plain = text => text;
  const createList = query => {
    const list = new SelectList(filterSessions(sessions, query).map(item => ({ value: item.application_session_id,
      label: item.title || item.application_session_id,
      description: `${sessionActivity(item)} · ${item.cwd} · ${item.application_session_id}` })), 12,
      { selectedPrefix: plain, selectedText: plain, description: plain, scrollInfo: plain,
        noMatch: () => '  没有匹配的会话' });
    list.onSelect = item => finish(item.value); list.onCancel = () => finish(null);
    return list;
  };
  let list = createList('');
  const input = new Input({ prompt: '搜索 › ', placeholder: '输入标题或会话 ID；↑↓ 选择；Enter 恢复；Esc 取消' });
  return {
    get focused() { return input.focused; },
    set focused(value) { input.focused = value; },
    render: width => ['KernelBridge · 恢复会话', '', ...input.render(width), '', ...list.render(width)],
    invalidate() { input.invalidate(); list.invalidate(); },
    handleInput(data) {
      if (matchesKey(data, 'escape') || matchesKey(data, 'ctrl+c') || matchesKey(data, 'ctrl+d')) return finish(null);
      if (['up', 'down', 'pageUp', 'pageDown', 'enter'].some(key => matchesKey(data, key))) list.handleInput(data);
      else { input.handleInput(data); list = createList(input.getValue()); }
      requestRender();
    },
  };
}

/** Native Pi widgets, no model calls and no host lease until a session is chosen. */
export async function pickSession(sessions) {
  if (!process.stdin.isTTY) throw new Error('Use an explicit session ID or --last outside a terminal');
  if (!sessions.length) throw new Error('没有可恢复的会话。用 --all 查看其他项目。');
  const tui = new TuiMainScreen(new ProcessTerminal());
  let finish;
  const result = new Promise(resolve => { finish = resolve; });
  const component = sessionPicker(sessions, finish, () => tui.requestRender());
  tui.addChild(component); tui.setFocus(component);
  try { tui.start(); return await result; }
  finally { tui.stop(); }
}
