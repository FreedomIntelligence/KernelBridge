import { TuiMainScreen, ProcessTerminal, SelectList, Input, matchesKey } from '@earendil-works/pi-tui';

/** Native Pi widgets, no model calls and no host lease until a session is chosen. */
export async function pickSession(sessions) {
  if (!process.stdin.isTTY) throw new Error('Use an explicit session ID or --last outside a terminal');
  if (!sessions.length) throw new Error('没有可恢复的会话。用 --all 查看其他项目。');
  const plain = text => text;
  const list = new SelectList(sessions.map(item => ({ value: item.application_session_id,
    label: item.title || item.application_session_id,
    description: `${item.cwd} · ${item.application_session_id}` })), 12,
    { selectedPrefix: plain, selectedText: plain, description: plain, scrollInfo: plain, noMatch: plain });
  const input = new Input({ prompt: '搜索 › ', placeholder: '输入标题或会话 ID；↑↓ 选择；Enter 恢复；Esc 取消' });
  const tui = new TuiMainScreen(new ProcessTerminal());
  let finish;
  const result = new Promise(resolve => { finish = resolve; });
  list.onSelect = item => finish(item.value); list.onCancel = () => finish(null);
  const component = {
    focused: true,
    render: width => ['KernelBridge · 恢复会话', '', ...input.render(width), '', ...list.render(width)],
    invalidate() { input.invalidate(); list.invalidate(); },
    handleInput(data) {
      if (matchesKey(data, 'escape') || matchesKey(data, 'ctrl+c') || matchesKey(data, 'ctrl+d')) return finish(null);
      if (['up', 'down', 'enter'].some(key => matchesKey(data, key))) list.handleInput(data);
      else { input.handleInput(data); list.setFilter(input.getValue()); }
      tui.requestRender();
    },
  };
  tui.addChild(component); tui.setFocus(component);
  try { tui.start(); return await result; }
  finally { tui.stop(); }
}
