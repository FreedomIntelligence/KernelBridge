import { CURSOR_MARKER, renderFakeCursor, truncateToWidth, visibleWidth, wrapTextWithAnsi } from '@earendil-works/pi-tui';
import { CustomEditor } from '@earendil-works/pi-coding-agent';
import { homedir } from 'node:os';
import { childMetrics, delegateTitle, reasonLabel } from './tool-render.mjs';

const STATUSES = { active: '进行中', paused: '已暂停', awaiting_user: '等待输入', completed: '已完成',
  budget_limit: '预算耗尽', failed: '失败', cleared: '已清除' };
const currentGoal = state => state.goal?.status === 'cleared' ? null : state.goal;
const plain = value => String(value ?? '').replace(/[\x00-\x1f\x7f-\x9f]/g, ' ');

export const runningChildren = state => (state?.subagents ?? []).filter(child => child.status === 'running');
/** Inputs the host accepted but has not started yet. */
export const queuedInputs = state => Object.values(state?.inputs ?? {}).filter(item => item.status === 'queued').length;

/**
 * Working-row text: elapsed time, live children, queued messages and the interrupt key.
 * With `columns`, details are dropped from the right so the row stays on one line;
 * the row itself adds a spinner and one column of padding on each side.
 */
/** Footer line for the active Goal's plan: progress and the step in progress. */
export function planFooter(plan) {
  const steps = plan?.plan ?? [];
  const done = steps.filter(item => item.status === 'completed').length;
  const active = steps.find(item => item.status === 'in_progress');
  return `计划 ${done}/${steps.length}` + (active ? ` · 正在：${active.step}` : done === steps.length ? ' · 已全部完成' : '');
}

export function workingMessage(state, startedAt, activity = '执行中', columns) {
  const seconds = Math.max(0, Math.floor((Date.now() - startedAt) / 1000));
  const elapsed = seconds < 60 ? `${seconds}s` : `${Math.floor(seconds / 60)}m${String(seconds % 60).padStart(2, '0')}s`;
  const children = runningChildren(state).length;
  const queued = queuedInputs(state);
  const head = children ? `等待 ${children} 个子代理完成` : activity;
  const counts = [elapsed, ...(queued ? [`${queued} 条排队`] : [])];
  const variants = [`${head}… (${[...counts, 'esc 中断'].join(' · ')})`, `${head}… (${counts.join(' · ')})`, `${head}… (${elapsed})`, `${head}…`];
  const room = columns ? columns - 4 : Infinity;
  return variants.find(text => visibleWidth(text) <= room) ?? truncateToWidth(head, Math.max(1, room), '…');
}

const CHILD_STATUS = { running: '运行中', completed: '完成', failed: '失败', unknown: '待恢复' };

/** One-line child status shared by /agents and tests; metrics come from host receipts. */
export function subagentStatus(child) {
  const status = child.partial ? `部分结果（${reasonLabel(child.reason)}）`
    : child.status === 'failed' ? `失败${child.reason ? '：' + reasonLabel(child.reason) : ''}` : CHILD_STATUS[child.status] ?? child.status;
  const elapsed = child.elapsed_ms ?? child.duration_ms ?? (child.started_at_ns == null ? undefined
    : Math.max(0, (child.ended_at_ns == null ? Date.now() : Number(child.ended_at_ns) / 1e6) - Number(child.started_at_ns) / 1e6));
  return plain([child.agent, status, childMetrics(child, elapsed), delegateTitle(child)].filter(Boolean).join(' · '));
}

export function goalSummary(state) {
  const goal = currentGoal(state);
  const budget = state.budget ?? {};
  return [goal ? `Goal ${STATUSES[goal.status] ?? goal.status}` : '普通会话',
    ...(state.workspace_mode !== 'project' ? [`task v${state.task_version}`, `Trial ${state.trials?.length || 0}`] : []),
    ...(budget.limit == null ? [] : [`credits ${budget.spent ?? 0} + ${budget.reserved ?? 0} / ${budget.limit}`]),
    ...(goal ? [`detach=${goal.on_detach ?? 'pause'}`] : []),
    ...(state.pending_effect_ids?.length ? [`pending=${state.pending_effect_ids.length}`] : [])].join(' · ');
}

export function statePresentation(state) {
  const goal = currentGoal(state);
  const budget = state.budget ?? {};
  const criteria = state.task?.contract?.acceptance;
  // Labels are two characters wide, so values already align. The feedback block
  // elides the path's middle at the real terminal width (fitFeedback).
  return [goal ? `目标：${goal.objective}` : '尚未设置 Goal。输入 /goal 目标内容 开始持续执行。',
    `状态：${goal ? STATUSES[goal.status] ?? goal.status : '普通会话'}`,
    ...(state.cwd ? [`目录：${shortPath(state.cwd)}`, `模型：${state.model}`] : []),
    ...(state.workspace_mode !== 'project' ? [`任务版本 ${state.task_version} · Trial ${state.trials?.length || 0}`] : []),
    ...(goal && state.goal_usage ? [`已用时 ${state.goal_usage.elapsed_seconds}s · 模型调用 ${state.goal_usage.model_turns} · tokens ${state.goal_usage.tokens}${goal.token_budget == null ? '' : '/' + goal.token_budget}`,
      ...(state.goal_usage.usage_unknown_calls ? [`${state.goal_usage.usage_unknown_calls} 次调用未报告 token 用量`] : [])] : []),
    ...(goal?.reason ? [`原因：${goal.reason}`] : []),
    ...(goal?.summary ? [`说明：${goal.summary}`] : []),
    ...(budget.limit == null ? [] : [`预算：${budget.spent ?? 0}/${budget.limit} · 预留 ${budget.reserved ?? 0}`]),
    ...(criteria ? [`验收：${criteria.evidence_tier}${criteria.target == null ? '' : ` · 目标 ${criteria.target}`}`] : []),
    ...(state.best ? [`最佳已验收候选：${state.best.evidence_tier} · ${state.best.source_sha256?.slice(0, 12) ?? ''}`] : []),
    ...(goal ? [`退出策略：${goal.on_detach === 'continue' ? '后台继续' : '暂停'}`] : [])].join('\n');
}

const home = homedir();
const shortPath = path => path && home && (path === home || path.startsWith(home + '/')) ? '~' + path.slice(home.length) : path ?? '';

/** Keeps the directory's own name visible: the middle is elided, not the tail. */
export function elidePath(path, width) {
  const text = shortPath(path);
  if (!width || visibleWidth(text) <= width) return text;
  const parts = text.split('/');
  const last = parts[parts.length - 1];
  for (let keep = parts.length - 1; keep > 0; keep--) {
    const candidate = [...parts.slice(0, keep), '…', last].join('/');
    if (visibleWidth(candidate) <= width) return candidate;
  }
  return truncateToWidth('…/' + last, width, '…');
}

/** Left and right segments on one row; the left side is truncated first. */
function split(left, right, width) {
  const gap = 2;
  const rightWidth = visibleWidth(right);
  if (!right || rightWidth + gap >= width) return truncateToWidth(left || right, width, '…');
  const fitted = truncateToWidth(left, width - rightWidth - gap, '…');
  return fitted + ' '.repeat(Math.max(gap, width - visibleWidth(fitted) - rightWidth)) + right;
}

/** Claude Code-style welcome box: product, quick commands and the working directory. */
export function welcomeBox(state, theme, width, { demo = false } = {}) {
  const color = (name, text) => theme?.fg(name, text) ?? text;
  const project = state?.workspace_mode === 'project';
  const rows = [
    `${color('accent', '✻')} ${theme?.bold?.('KernelBridge') ?? 'KernelBridge'}`,
    ...(demo ? [color('warning', '本地演示：固定脚本回复，无真实模型或 NPU')] : []),
    '',
    color('muted', '  ' + (project ? '/help 命令 · /agents 子代理 · /model 模型 · ! shell' : '/goal 目标内容 持续执行 · /help 命令 · /agents 子代理')),
    color('muted', '  esc 中断 · ctrl+o 展开工具输出 · /resume 历史会话'),
    ...(state?.cwd ? ['', color('dim', `  cwd: ${shortPath(state.cwd)}`)] : []),
  ];
  const inner = Math.min(Math.max(20, width - 4), 68);
  if (width < 24) return rows.filter(Boolean).map(row => truncateToWidth(row, width, '…'));
  const border = text => color('borderMuted', text);
  return [border('╭' + '─'.repeat(inner + 2) + '╮'),
    ...rows.map(row => { const cell = truncateToWidth(row, inner, '…'); return border('│ ') + cell + ' '.repeat(Math.max(0, inner - visibleWidth(cell))) + border(' │'); }),
    border('╰' + '─'.repeat(inner + 2) + '╯')];
}

/** The fullest right-hand text that still leaves the left side whole. */
function fitRight(left, rights, width) {
  const right = rights.find(text => !text || visibleWidth(left) + visibleWidth(text) + 2 <= width) ?? rights.at(-1);
  return split(left, right, width);
}

/**
 * The branch and the right-hand hint stay whole while the path gives up room
 * (down to eight columns). Narrower rows drop the detach label, then shorten the branch.
 */
function bottom(state, branch, detach, color, width) {
  const tail = branch ? ` (${branch})` : '';
  for (const right of [...new Set([`${detach}/help`, '/help'])]) {
    const room = width - visibleWidth(right) - 2 - visibleWidth(tail);
    if (room >= 8) return split(color('dim', elidePath(state.cwd, room) + tail), color('dim', right), width);
  }
  const branchRoom = Math.floor(width / 2) - 3;
  const shortTail = branch && branchRoom >= 4 ? ` (${truncateToWidth(branch, branchRoom, '…')})` : '';
  return truncateToWidth(color('dim', elidePath(state.cwd, Math.max(4, width - visibleWidth(shortTail))) + shortTail), width, '…');
}

/** Empty-editor hints, widest first; the widest that fits is shown (Codex composer style). */
const HINTS = {
  project: ['试试：修复失败的测试 · / 命令 · ! shell', '试试：修复失败的测试 · / 命令', '输入任务 · / 命令', '输入任务'],
  task: ['输入约束或问题 · /goal 目标内容 持续执行', '输入约束或问题 · / 命令', '输入约束或问题'],
};

export function editorPlaceholder(mode, width) {
  return HINTS[mode === 'project' ? 'project' : 'task'].find(text => visibleWidth(text) <= width) ?? '';
}

/** Pi's own editor plus a dim hint while it is empty; any text or open completion hides it. */
export function placeholderEditor(getState, getTheme) {
  class PlaceholderEditor extends CustomEditor {
    render(width) {
      const lines = super.render(width);
      if (this.getText() || this.isShowingAutocomplete() || lines.length < 3) return lines;
      const padding = Math.min(this.getPaddingX(), Math.max(0, Math.floor((width - 1) / 2)));
      const content = Math.max(1, width - padding * 2);
      const hint = editorPlaceholder(getState()?.workspace_mode, content - 2);
      if (!hint) return lines;
      const theme = getTheme();
      const cursor = (this.focused ? CURSOR_MARKER : '') + renderFakeCursor(' ');
      lines[1] = ' '.repeat(padding) + cursor + ' ' + (theme?.fg?.('dim', hint) ?? hint)
        + ' '.repeat(Math.max(0, content - 2 - visibleWidth(hint))) + ' '.repeat(padding);
      return lines;
    }
  }
  return (tui, editorTheme, keybindings) => new PlaceholderEditor(tui, editorTheme, keybindings);
}

/** Product status stays visible without exposing broker IDs, synthetic token limits or plugin internals. */
export function installProductUI(context, getState, { demo = false } = {}) {
  context.ui.setEditorComponent?.(placeholderEditor(getState, () => context.ui.theme));
  context.ui.setHeader((_tui, theme) => ({
    invalidate() {},
    render: width => ['', ...welcomeBox(getState(), theme, width, { demo })],
  }));
  context.ui.setFooter((_tui, theme, footerData) => ({
    invalidate() {},
    render(width) {
      const state = getState();
      if (!state) return [];
      const goal = currentGoal(state), budget = state.budget ?? {};
      const color = (name, text) => theme?.fg(name, text) ?? text;
      const branch = footerData?.getGitBranch();
      const children = runningChildren(state).length;
      const busy = Object.values(state.inputs ?? {}).some(item => item.status === 'applied');
      const activity = goal ? `Goal ${STATUSES[goal.status] ?? goal.status}` : busy ? '执行中' : '等待输入';
      const usage = context.getContextUsage?.();
      const percent = usage?.percent ?? (usage?.tokens != null && usage?.contextWindow ? usage.tokens / usage.contextWindow * 100 : null);
      const left = state.workspace_mode === 'project'
        ? `${color(busy || children ? 'accent' : 'success', '●')} ${color('text', state.model ?? '未配置模型')} ${color('dim', '·')} ${color('muted', activity)}`
        : `${color('accent', '●')} ${color('muted', `${goal ? `Goal ${STATUSES[goal.status] ?? goal.status}` : 'Session'} · task v${state.task_version} · Trial ${state.trials?.length || 0}`)}`;
      const remaining = Math.max(0, Math.round(100 - percent));
      const rights = percent == null ? [''] : [`上下文剩余 ${remaining}%`, `剩余 ${remaining}%`].map(text => color(percent >= 80 ? 'warning' : 'dim', text));
      const detach = goal || state.workspace_mode !== 'project' ? `退出${goal?.on_detach === 'continue' ? '继续' : '暂停'} · ` : '';
      return [
        fitRight(left, rights, width),
        ...(children ? [truncateToWidth(color('accent', `子代理 ${children} 个运行中 · /agents 查看`), width, '…')] : []),
        ...(goal && state.plan?.plan?.length ? [truncateToWidth(color('muted', planFooter(state.plan)), width, '…')] : []),
        ...(budget.limit == null ? [] : [truncateToWidth(color('muted', [`预算 ${budget.spent ?? 0}/${budget.limit}`,
          ...(budget.reserved ? [`预留 ${budget.reserved}`] : []),
          ...(state.pending_effect_ids?.length ? [`待处理 ${state.pending_effect_ids.length}`] : [])].join(' · ')), width, '…')]),
        bottom(state, branch, detach, color, width),
      ];
    },
  }));
}

const LABELLED_PATH = /^([^\s：]{1,8}：)([~/]\S*)$/;
// Indented command, a gap of two or more spaces, then a description without such gaps.
const TABLE_ROW = /^( {2,})(\S.*?)( {2,})(\S(?:(?! {2}).)*)$/;

/** Wraps at ` · ` separators first, so a list of hints never breaks inside an item. */
function wrapIndented(line, width) {
  const text = line.trimStart();
  let indent = line.length - text.length;
  if (indent + visibleWidth(text) > width) indent = Math.min(indent, 4);
  const room = Math.max(1, width - indent);
  const parts = [];
  for (const segment of text.split(' · ')) {
    const last = parts.length - 1;
    if (last >= 0 && visibleWidth(parts[last]) + 3 + visibleWidth(segment) <= room) parts[last] += ' · ' + segment;
    else parts.push(...wrapTextWithAnsi(segment, room));
  }
  return parts.map(part => ' '.repeat(indent) + part);
}

/**
 * Host blocks at the real terminal width. A labelled path keeps its label and
 * elides its middle. A two-column table first tightens its column for every row
 * at once, then puts each description under its command. Other lines wrap with
 * their own indent.
 */
export function fitFeedback(text, width) {
  const lines = String(text).split('\n');
  const rows = lines.map(line => TABLE_ROW.exec(line));
  const table = rows.filter(Boolean);
  let mode = 'as-is', column = 0, indent = 2;
  if (table.some(([row]) => visibleWidth(row) > width)) {
    column = Math.max(...table.map(([, , command]) => visibleWidth(command)));
    indent = Math.max(...table.map(([, lead]) => lead.length));
    mode = table.every(([, , , , description]) => indent + column + 2 + visibleWidth(description) <= width) ? 'tight' : 'stacked';
  }
  return lines.flatMap((line, index) => {
    const row = rows[index];
    if (row && mode !== 'as-is') {
      const [, lead, command, , description] = row;
      if (mode === 'tight') return [lead + command + ' '.repeat(column - visibleWidth(command) + 2) + description];
      const inline = lead + command + '  ' + description;
      return visibleWidth(inline) <= width ? [inline]
        : [...wrapIndented(lead + command, width), ...wrapIndented(' '.repeat(lead.length + 2) + description, width)];
    }
    // A description on its own line, under a command wider than the column.
    if (mode === 'tight' && /^ {6,}\S/.test(line) && indent + column + 2 + visibleWidth(line.trim()) <= width) {
      return [' '.repeat(indent + column + 2) + line.trim()];
    }
    if (visibleWidth(line) <= width) return [line];
    const path = LABELLED_PATH.exec(line);
    return path ? [path[1] + elidePath(path[2], Math.max(4, width - visibleWidth(path[1])))] : wrapIndented(line, width);
  });
}

/** Host feedback is marked so it reads as the product speaking, not model output. */
export function showProductFeedback(context, message, error = false) {
  if (!context) return;
  context.ui.setWidget('kernelbridge-feedback', message ? (tui, theme) => ({
    invalidate() {},
    render(width) {
      const lines = String(message).split('\n');
      // The marker goes on the first line only: blocks such as /help keep their own alignment.
      const body = [(error ? '✗ ' : '⏺ ') + lines[0], ...lines.slice(1)].join('\n');
      const rows = fitFeedback(body, width).map(row => theme.fg(error ? 'error' : 'accent', row));
      // Blocks such as /help use the height the editor and footer leave free.
      const limit = Math.max(3, (tui.terminal?.rows ?? 24) - 7);
      return rows.length <= limit ? rows : [...rows.slice(0, limit - 1), '…'];
    },
  }) : undefined);
}
