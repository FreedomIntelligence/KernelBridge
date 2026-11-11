import { Text, truncateToWidth } from '@earendil-works/pi-tui';
import { childMetrics, delegateTitle, reasonLabel } from './tool-render.mjs';

const STATUSES = { active: '进行中', paused: '已暂停', awaiting_user: '等待输入', completed: '已完成',
  budget_limit: '预算耗尽', failed: '失败', cleared: '已清除' };
const currentGoal = state => state.goal?.status === 'cleared' ? null : state.goal;
const plain = value => String(value ?? '').replace(/[\x00-\x1f\x7f-\x9f]/g, ' ');

export const runningChildren = state => (state?.subagents ?? []).filter(child => child.status === 'running');

/** Working-row text: elapsed time, live children and the interrupt key. */
export function workingMessage(state, startedAt) {
  const seconds = Math.max(0, Math.floor((Date.now() - startedAt) / 1000));
  const elapsed = seconds < 60 ? `${seconds}s` : `${Math.floor(seconds / 60)}m${String(seconds % 60).padStart(2, '0')}s`;
  const children = runningChildren(state).length;
  return `${children ? `等待 ${children} 个子代理完成` : '执行中'}… (${elapsed} · Esc 中断)`;
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
  return [goal ? `目标：${goal.objective}` : '尚未设置 Goal。输入 /goal 目标内容 开始持续执行。',
    `状态：${goal ? STATUSES[goal.status] ?? goal.status : '普通会话'}`,
    ...(state.cwd ? [`目录：${state.cwd}`, `模型：${state.model}`] : []),
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

/** Product status stays visible without exposing broker IDs, synthetic token limits or plugin internals. */
export function installProductUI(context, getState, { demo = false } = {}) {
  const project = getState()?.workspace_mode === 'project';
  context.ui.setHeader(project ? undefined : () => new Text([
    'KernelBridge',
    ...(demo ? ['本地演示：固定脚本回复，无真实模型或 NPU。'] : []),
    '输入任务，或 /goal 目标内容 · /help 命令 · /latest 最新消息',
  ].join('\n'), 0, 1));
  if (project && demo) context.ui.setWidget('kernelbridge-demo', ['本地演示：固定脚本回复，无真实模型或 NPU。']);
  context.ui.setFooter((_tui, theme, footerData) => ({
    invalidate() {},
    render(width) {
      const state = getState();
      if (!state) return [];
      const goal = currentGoal(state), budget = state.budget ?? {};
      const children = runningChildren(state).length;
      const activity = Object.values(state.inputs ?? {}).some(item => item.status === 'applied') ? '执行中'
        : children ? '后台任务运行中' : '等待输入';
      const color = (name, text) => theme?.fg(name, text) ?? text;
      const branch = footerData?.getGitBranch();
      return new Text([
        ...(state.cwd ? [color('dim', state.cwd + (branch ? ` (${branch})` : ''))] : []),
        state.workspace_mode === 'project'
          ? `${color('accent', state.model ?? '未配置模型')} · ${goal ? 'Goal ' + (STATUSES[goal.status] ?? goal.status) : color('dim', activity)}`
          : `${goal ? `Goal ${STATUSES[goal.status] ?? goal.status}` : 'Session'} · task v${state.task_version} · Trial ${state.trials?.length || 0}`,
        ...(budget.limit == null ? [] : [`预算 ${budget.spent ?? 0}/${budget.limit} · 预留 ${budget.reserved ?? 0}`
          + (state.workspace_mode !== 'project' ? ` · 待处理 ${state.pending_effect_ids?.length ?? 0}` : '')]),
        ...(children ? [color('accent', truncateToWidth(`等待 ${children} 个子代理完成 · /agents 查看`, width))] : []),
        `${goal || state.workspace_mode !== 'project' ? `退出${goal?.on_detach === 'continue' ? '继续' : '暂停'} · ` : ''}/help · /latest`,
      ].join('\n'), 0, 0).render(width);
    },
  }));
}

export function showProductFeedback(context, message, error = false) {
  if (!context) return;
  context.ui.setWidget('kernelbridge-feedback', message ? (tui, theme) => ({
    invalidate() {},
    render(width) {
      const rows = new Text(theme.fg(error ? 'error' : 'accent', String(message)), 0, 0).render(width);
      const limit = Math.max(3, Math.floor(tui.terminal.rows / 2));
      return rows.length <= limit ? rows : [...rows.slice(0, limit - 1), '…'];
    },
  }) : undefined);
}
