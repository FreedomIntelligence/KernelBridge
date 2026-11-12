// Read-only projection of a Pi session branch into the Lattice Trace step model
// (kernelbridge.trace/v1). Input is what an extension can read from
// ctx.sessionManager (getHeader(), getBranch()) plus the live state Pi has not
// persisted yet: tools that are executing and an assistant reply that is streaming.
import {
  MAIN, TRACE_SCHEMA, clip, clipped, contentText, finishSteps, firstLine, recordFileChange, round2, summarize, text, tokens, toolTitle,
  type Json, type Step, type Trace, type TraceLabels,
} from './trace-core.ts';

export interface RunningTool { tool: string; args: Json; startedAt: number }
export interface SessionTraceOptions {
  header?: Json;
  /** Tool calls between tool_execution_start and their persisted result, by toolCallId. */
  running?: Map<string, RunningTool>;
  /** Start time (ms) of an assistant reply that is still streaming. */
  streamingSince?: number | null;
  model?: { provider?: string; id?: string; contextWindow?: number } | null;
  thinkingLevel?: string | null;
  reserveTokens?: number;
  sessionName?: string | null;
  now?: number;
  live?: boolean;
  labels?: TraceLabels;
}

export const PI_LABELS: TraceLabels = {
  harness: 'Pi', eyebrow: 'Pi · 实时会话轨迹', source: '会话记录（只读）', defaultTitle: 'Pi 会话',
  workspace: '项目目录（直接修改）',
  filesNote: 'Pi 不快照工作区；文件内容由本会话的写入与修改调用重建，只覆盖这些调用涉及的部分。',
  childNote: '子代理（pi-subagents）的会话暂未合并到时间线。',
};

const ms = (value: Json): number | null => {
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  if (typeof value === 'string') { const parsed = Date.parse(value); return Number.isNaN(parsed) ? null : parsed; }
  return null;
};

export function projectSession(entries: Json[], options: SessionTraceOptions = {}): Trace {
  const now = options.now ?? Date.now();
  const header = options.header ?? {};
  const base = ms(header.timestamp) ?? ms(entries[0]?.timestamp) ?? now;
  const at = (time: number | null) => round2(Math.max(0, ((time ?? now) - base) / 1000));
  const running = options.running ?? new Map<string, RunningTool>();

  const steps: Step[] = []; const commits: Json[] = []; const files: Record<string, [string, string][]> = {};
  const calls = new Map<string, { step: Step; tool: string; args: Json }>();
  const labels = new Map<string, string>();
  const tools = new Set<string>();
  let model: string | null = options.model?.id ? [options.model.provider, options.model.id].filter(Boolean).join('/') : null;
  let thinking = options.thinkingLevel ?? null;
  let name = options.sessionName ?? ''; let firstPrompt = '';
  let ctx = 0; let turnNo = 0; let started = false; let updated: number | null = null;
  const marker = (id: string, time: number | null, title: string, note: string) => {
    if (!started) return; // the session header's own model and thinking entries precede any input
    const t = at(time);
    steps.push({ id: 'C' + id, agent: MAIN, branch: 'M', src: 'harness', kind: 'checkpoint', t0: t, t1: t, title, status: 'ok', note, ctx });
  };
  const input = (id: string, time: number | null, title: string, note: string, addTok: number) => {
    started = true; const t = at(time);
    steps.push({ id: 'U' + id, agent: MAIN, branch: 'M', src: 'harness', kind: 'input', t0: t, t1: round2(t + 0.05), title, status: 'ok', note: clip(note, 2000), ctx: 0, addTok });
  };

  for (const entry of entries) {
    const time = ms(entry.timestamp); if (time !== null) updated = time;
    if (entry.type === 'session_info' && entry.name) name = entry.name;
    else if (entry.type === 'label' && entry.targetId) { if (entry.label) labels.set(entry.targetId, entry.label); else labels.delete(entry.targetId); }
    else if (entry.type === 'model_change') { model = [entry.provider, entry.modelId].filter(Boolean).join('/'); marker(entry.id, time, '切换模型 ' + model, '之后的请求使用 ' + model + '。'); }
    else if (entry.type === 'thinking_level_change') { thinking = entry.thinkingLevel ?? thinking; marker(entry.id, time, '思考级别 ' + entry.thinkingLevel, '之后的请求使用思考级别 ' + entry.thinkingLevel + '。'); }
    else if (entry.type === 'context_edit') marker(entry.id, time, '上下文编辑', '从这一步起，发给模型的上下文按这次编辑调整。');
    else if (entry.type === 'branch_summary') {
      const t = at(time);
      steps.push({ id: 'B' + entry.id, agent: MAIN, branch: 'M', src: 'harness', kind: 'checkpoint', t0: t, t1: round2(t + 0.05), title: '从另一分支返回', status: 'ok',
        note: clip(text(entry.summary)), ctx, addTok: tokens(text(entry.summary)) });
    } else if (entry.type === 'compaction') {
      const t = at(time); const before = entry.tokensBefore ?? ctx;
      steps.push({ id: 'K' + entry.id, agent: MAIN, branch: 'M', src: 'harness', kind: 'compact', t0: t, t1: round2(t + 0.05), title: '', status: 'warn',
        note: clip(text(entry.summary)), before, after: tokens(text(entry.summary)), ctx: 0 });
    } else if (entry.type === 'custom_message' && entry.display !== false) {
      const body = contentText(entry.content);
      input(entry.id, time, '通知：' + (entry.customType ?? '插件'), body, tokens(body));
    } else if (entry.type === 'message') {
      const m = entry.message ?? {};
      if (m.role === 'system') {
        for (const tool of m.toolsAdded ?? []) if (tool?.name) tools.add(tool.name);
        for (const tool of m.toolsRemoved ?? []) if (tool?.name) tools.delete(tool.name);
      } else if (m.role === 'user') {
        const body = contentText(m.content); firstPrompt ||= body;
        input(entry.id, ms(m.timestamp) ?? time, '用户：' + firstLine(body, 40), body, tokens(body));
      } else if (m.role === 'bashExecution') {
        const body = `$ ${m.command}\n${m.output ?? ''}`;
        input(entry.id, ms(m.timestamp) ?? time, (m.excludeFromContext ? '命令（不进上下文） ' : '命令 ') + firstLine(text(m.command), 36), body, m.excludeFromContext ? 0 : tokens(body));
      } else if (m.role === 'custom' && m.display !== false) {
        const body = contentText(m.content);
        input(entry.id, ms(m.timestamp) ?? time, '通知：' + (m.customType ?? '插件'), body, tokens(body));
      } else if (m.role === 'assistant') {
        started = true;
        const end = time ?? now; const begin = Math.min(ms(m.timestamp) ?? end, end);
        const usage = m.usage ?? {};
        const said = contentText((m.content ?? []).filter((block: Json) => block?.type !== 'toolCall'));
        const toolCalls = (m.content ?? []).filter((block: Json) => block?.type === 'toolCall');
        const failed = m.stopReason === 'error' || m.stopReason === 'aborted';
        const reported = typeof usage.input === 'number';
        const tokIn = reported ? (usage.input ?? 0) + (usage.cacheRead ?? 0) + (usage.cacheWrite ?? 0) : ctx;
        const tokOut = reported ? usage.output ?? 0 : said ? tokens(said) : 0;
        const names = toolCalls.map((call: Json) => call.name);
        const step: Step = { id: 'T' + entry.id, agent: MAIN, branch: 'M', src: 'model', kind: !toolCalls.length && !failed ? 'final' : 'think',
          t0: at(begin), t1: round2(Math.max(at(end), at(begin) + 0.01)),
          title: failed ? (m.stopReason === 'aborted' ? '模型调用被中断' : '模型调用失败') : firstLine(said) || (names.length ? '调用 ' + [...new Set(names)].join('、') : '思考'),
          status: failed ? 'error' : 'ok', tokIn, tokOut, cacheRead: 0, cacheReal: reported ? usage.cacheRead ?? 0 : null,
          cacheWrite: reported ? usage.cacheWrite ?? 0 : null, reasoningOut: reported ? usage.reasoning ?? null : null,
          turnNo: ++turnNo, thought: clip(failed ? text(m.errorMessage ?? m.stopReason) + (said ? '\n\n' + said : '') : said), ctx: tokIn + tokOut,
          model: m.responseModel ?? m.model ?? null };
        if (m.model) model = [m.provider, m.model].filter(Boolean).join('/');
        steps.push(step); ctx = tokIn + tokOut;
        toolCalls.forEach((call: Json, index: number) => {
          const live = running.get(call.id);
          const tool: Step = { id: `T${entry.id}.${index}`, agent: MAIN, branch: 'M', src: 'tool', kind: 'call', t0: step.t1,
            t1: live ? round2(Math.max(at(now), step.t1 + 0.05)) : round2(step.t1 + 0.01), title: toolTitle(call.name, call.arguments),
            status: live ? 'running' : 'warn', tool: call.name, callId: call.id, args: call.arguments ?? {}, out: live ? '' : '（没有记录结果：调用未完成或被中断）', outTok: 0, turn: step.id, ctx };
          steps.push(tool); calls.set(call.id, { step: tool, tool: call.name, args: call.arguments ?? {} });
        });
      } else if (m.role === 'toolResult') {
        const call = calls.get(m.toolCallId); if (!call) continue;
        const step = call.step; const output = clipped(contentText(m.content)); const body = output.out;
        step.t1 = round2(Math.max(at(ms(m.timestamp) ?? time), step.t0 + 0.01));
        step.status = m.isError ? 'error' : 'ok'; Object.assign(step, output); step.outTok = body ? tokens(body) : 0;
        ctx += step.outTok; step.ctx = ctx;
        if (!m.isError) recordFileChange(step, call.tool, call.args, files, commits);
      }
    }
  }
  if (options.streamingSince != null) {
    started = true; const t0 = at(options.streamingSince);
    steps.push({ id: 'T~live', agent: MAIN, branch: 'M', src: 'model', kind: 'think', t0, t1: round2(Math.max(at(now), t0 + 0.05)), title: '生成中…', status: 'running',
      tokIn: ctx, tokOut: 0, cacheRead: 0, cacheReal: null, turnNo: turnNo + 1, thought: '', ctx });
  }
  for (const step of steps) { const id = step.id.slice(1).split('.')[0]; const label = labels.get(id); if (label && !step.id.includes('.')) step.title += ` [${label}]`; }

  // A compaction's resulting size is the next request's prompt minus what was added in between.
  steps.forEach((step, index) => {
    if (step.kind !== 'compact') return;
    let added = 0;
    for (const next of steps.slice(index + 1)) {
      if (next.src === 'model') { if (next.status !== 'running' && next.tokIn) step.after = Math.max(1, next.tokIn - added); break; }
      added += next.src === 'tool' ? next.outTok ?? 0 : next.addTok ?? 0;
    }
    step.title = `上下文压缩 ${(step.before / 1000).toFixed(1)}k → ${(step.after / 1000).toFixed(1)}k`;
  });

  finishSteps(steps);
  const summary = summarize(steps, 1);
  const limit = options.model?.contextWindow ?? 128000;
  const reserve = options.reserveTokens ?? 16384;
  const labelSet = options.labels ?? PI_LABELS;
  return {
    schema: TRACE_SCHEMA, seq: entries.length, live: options.live ?? true,
    session: { id: header.id ?? null, title: firstLine(name || firstPrompt, 120) || labelSet.defaultTitle || 'Pi 会话', model, cwd: header.cwd ?? null,
      started_ns: base * 1e6, updated_ns: updated === null ? null : updated * 1e6, status: summary.status },
    limit, thresh: Math.max(0, limit - reserve), budget: 0,
    config: { adapter: 'Pi', thinking, tools: [...tools], execution: '并行工具调用', workspace: 'project', compaction: `剩余 ${Math.round(reserve / 1000)}k 时压缩` },
    agents: { [MAIN]: { branch: 'M', name: '主会话', short: '主会话', tag: 'main', level: 0, sb: '—', ws: '—' } }, rows: [MAIN],
    branches: { M: { name: '当前分支', variant: model ?? 'Pi' } },
    steps, commits, files, result: summary.result, labels: labelSet,
  };
}
