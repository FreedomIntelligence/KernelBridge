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
  /** Every entry of the session tree (getEntries()); other branches become fork lanes. */
  tree?: Json[] | null;
  /** Entries of a subagent's own session when its result carries no messages (pi-subagents sessionFile). */
  childEntries?: ((result: Json) => Json[] | null) | null;
  /** Internal: project onto another lane (a subagent), on the parent's clock, with prefixed step ids. */
  lane?: string; baseMs?: number; idPrefix?: string; children?: boolean;
  now?: number;
  live?: boolean;
  labels?: TraceLabels;
}

export const PI_LABELS: TraceLabels = {
  harness: 'Pi', eyebrow: 'Pi · 实时会话轨迹', source: '会话记录（只读）', defaultTitle: 'Pi 会话',
  workspace: '项目目录（直接修改）',
  filesNote: 'Pi 不快照工作区；文件内容由本会话的写入与修改调用重建，只覆盖这些调用涉及的部分。',
  childNote: '来自 pi-subagents 的结果记录',
};

/** A subagent's messages as session entries; an assistant message ends when the next one starts. */
function messageEntries(messages: Json[], lane: string): Json[] {
  return messages.map((message: Json, index: number) => {
    const next = messages[index + 1]?.timestamp;
    const end = message.role === 'assistant' && typeof next === 'number' ? next : message.timestamp;
    return { type: 'message', id: `${lane}m${index}`, parentId: index ? `${lane}m${index - 1}` : null,
      timestamp: typeof end === 'number' ? new Date(end).toISOString() : undefined, message };
  });
}

const ms = (value: Json): number | null => {
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  if (typeof value === 'string') { const parsed = Date.parse(value); return Number.isNaN(parsed) ? null : parsed; }
  return null;
};

function projectBranch(entries: Json[], options: SessionTraceOptions = {}): Trace {
  const now = options.now ?? Date.now();
  const header = options.header ?? {};
  const base = options.baseMs ?? ms(header.timestamp) ?? ms(entries[0]?.timestamp) ?? now;
  const lane = options.lane ?? MAIN; const pid = options.idPrefix ?? '';
  const children: { step: Step; results: Json[]; entryId: string }[] = [];
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
    steps.push({ id: 'C' + pid + id, agent: lane, branch: 'M', src: 'harness', kind: 'checkpoint', t0: t, t1: t, title, status: 'ok', note, ctx });
  };
  const input = (id: string, time: number | null, title: string, note: string, addTok: number) => {
    started = true; const t = at(time);
    steps.push({ id: 'U' + pid + id, agent: lane, branch: 'M', src: 'harness', kind: 'input', t0: t, t1: round2(t + 0.05), title, status: 'ok', note: clip(note, 2000), ctx: 0, addTok });
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
      steps.push({ id: 'B' + pid + entry.id, agent: lane, branch: 'M', src: 'harness', kind: 'checkpoint', t0: t, t1: round2(t + 0.05), title: '从另一分支返回', status: 'ok',
        note: clip(text(entry.summary)), ctx, addTok: tokens(text(entry.summary)) });
    } else if (entry.type === 'compaction') {
      const t = at(time); const before = entry.tokensBefore ?? ctx;
      steps.push({ id: 'K' + pid + entry.id, agent: lane, branch: 'M', src: 'harness', kind: 'compact', t0: t, t1: round2(t + 0.05), title: '', status: 'warn',
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
        const step: Step = { id: 'T' + pid + entry.id, agent: lane, branch: 'M', src: 'model', kind: !toolCalls.length && !failed ? 'final' : 'think',
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
          const tool: Step = { id: `T${pid}${entry.id}.${index}`, agent: lane, branch: 'M', src: 'tool', kind: 'call', t0: step.t1,
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
        if (options.children !== false && call.tool === 'subagent' && Array.isArray(m.details?.results) && m.details.results.length) children.push({ step, results: m.details.results, entryId: entry.id });
      }
    }
  }
  if (options.streamingSince != null) {
    started = true; const t0 = at(options.streamingSince);
    steps.push({ id: 'T' + pid + '~live', agent: lane, branch: 'M', src: 'model', kind: 'think', t0, t1: round2(Math.max(at(now), t0 + 0.05)), title: '生成中…', status: 'running',
      tokIn: ctx, tokOut: 0, cacheRead: 0, cacheReal: null, turnNo: turnNo + 1, thought: '', ctx });
  }
  // pi-subagents: each child of a subagent call gets its own lane, a spawn on the parent and a return.
  const childAgents: Record<string, Json> = {}; const childRows: string[] = [];
  for (const { step, results, entryId } of children) {
    const end = step.t1;
    results.forEach((result: Json, k: number) => {
      const childLane = `${lane.split('/')[0]}/c${childRows.length + 1}`;
      const agent = String(result.agent ?? 'subagent');
      const failed = result.exitCode !== 0 || result.error || result.interrupted || result.timedOut || result.stopped;
      childAgents[childLane] = { branch: 'M', name: '子代理 · ' + agent, short: agent, tag: agent, level: 1, parent: lane, sb: '—', ws: '—', task: text(result.task) };
      childRows.push(childLane);
      const spawn: Step = k === 0 ? step : { ...step, id: `${step.id}~${k}` };
      Object.assign(spawn, { kind: 'spawn', child: childLane, t1: round2(Math.min(end, spawn.t0 + 0.3)), title: '委派 ' + agent, out: '已派生 子代理 · ' + agent, outTok: 40 });
      if (k) steps.push(spawn);
      const output = clipped(text(result.finalOutput ?? result.error ?? ''));
      steps.push({ id: `R${pid}${entryId}~${k}`, agent: lane, branch: 'M', src: 'tool', kind: 'return', child: childLane, t0: end, t1: round2(end + 0.05),
        title: '子代理返回：' + agent, status: failed ? 'error' : 'ok', tool: 'subagent', args: { agent, task: result.task }, ...output, outTok: output.out ? tokens(output.out) : 0, ctx });
      const own = Array.isArray(result.messages) && result.messages.length ? messageEntries(result.messages, childLane) : options.childEntries?.(result) ?? [];
      if (!own.length) return;
      const projected = projectBranch(own, { now, live: options.live, baseMs: base, lane: childLane, idPrefix: childLane + ':' });
      steps.push(...projected.steps);
    });
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
  const summary = summarize(steps, 1 + childRows.length);
  const limit = options.model?.contextWindow ?? 128000;
  const reserve = options.reserveTokens ?? 16384;
  const labelSet = options.labels ?? PI_LABELS;
  return {
    schema: TRACE_SCHEMA, seq: entries.length, live: options.live ?? true,
    session: { id: header.id ?? null, title: firstLine(name || firstPrompt, 120) || labelSet.defaultTitle || 'Pi 会话', model, cwd: header.cwd ?? null,
      started_ns: base * 1e6, updated_ns: updated === null ? null : updated * 1e6, status: summary.status },
    limit, thresh: Math.max(0, limit - reserve), budget: 0,
    config: { adapter: 'Pi', thinking, tools: [...tools], execution: '并行工具调用', workspace: 'project', compaction: `剩余 ${Math.round(reserve / 1000)}k 时压缩` },
    agents: { [MAIN]: { branch: 'M', name: '主会话', short: '主会话', tag: 'main', level: 0, sb: '—', ws: '—' }, ...childAgents }, rows: [MAIN, ...childRows],
    branches: { M: { name: '当前分支', variant: model ?? 'Pi' } },
    steps, commits, files, result: summary.result, labels: labelSet,
  };
}

const FORK_LETTERS = ['B', 'C', 'D'];
const entryOf = (step: Step) => String(step.id).slice(1).split('.')[0];
const meaningful = (path: Json[]) => path.some(entry => ['message', 'custom_message', 'compaction'].includes(entry.type));

/** The latest entry on the current path that has other children, and the newest branch under each. */
export function findFork(tree: Json[], path: Json[]): { index: number; branches: Json[][] } | null {
  const onPath = new Set(path.map(entry => entry.id));
  const order = new Map(tree.map((entry, index) => [entry.id, index]));
  const byId = new Map(tree.map(entry => [entry.id, entry]));
  const children = new Map<string, Json[]>();
  for (const entry of tree) if (entry.parentId) children.set(entry.parentId, [...(children.get(entry.parentId) ?? []), entry]);
  const newestPath = (start: Json) => {
    let newest = start; const stack = [start];
    while (stack.length) {
      const entry = stack.pop()!;
      if ((order.get(entry.id) ?? -1) > (order.get(newest.id) ?? -1)) newest = entry;
      stack.push(...(children.get(entry.id) ?? []));
    }
    const branch: Json[] = [];
    for (let entry: Json | undefined = newest; entry; entry = entry.id === start.id ? undefined : byId.get(entry.parentId)) branch.push(entry);
    return branch.reverse();
  };
  for (let index = path.length - 1; index >= 0; index--) {
    const others = (children.get(path[index].id) ?? []).filter(entry => !onPath.has(entry.id));
    const branches = others.map(newestPath).filter(meaningful)
      .sort((a, b) => (order.get(b.at(-1).id) ?? 0) - (order.get(a.at(-1).id) ?? 0)).slice(0, FORK_LETTERS.length);
    if (branches.length) return { index, branches };
  }
  return null;
}

export function projectSession(entries: Json[], options: SessionTraceOptions = {}): Trace {
  const trace = projectBranch(entries, options);
  const fork = options.tree ? findFork(options.tree, entries) : null;
  if (!fork) return trace;
  const position = new Map(entries.map((entry, index) => [entry.id, index]));
  const root = trace.steps.filter(step => step.agent === MAIN && (position.get(entryOf(step)) ?? Infinity) <= fork.index);
  const at = root.at(-1);
  if (!at) return trace;
  trace.fork = { t: at.t1, ctx: at.ctx ?? 0, step: at.id };
  fork.branches.forEach((branch, index) => {
    const letter = FORK_LETTERS[index]; const lane = letter + '/main';
    const own = new Set(branch.map(entry => entry.id));
    const projected = projectBranch([...entries.slice(0, fork.index + 1), ...branch],
      { ...options, tree: null, running: new Map(), streamingSince: null, children: false });
    const steps = projected.steps.filter(step => own.has(entryOf(step))).map(step => ({ ...step, agent: lane, branch: letter }));
    if (!steps.length) return;
    const prompt = branch.find(entry => entry.type === 'message' && entry.message?.role === 'user');
    trace.agents[lane] = { branch: letter, name: `${letter} · 另一分支`, short: '主会话', tag: letter, level: 0, fork: true, sb: '—', ws: '—' };
    trace.rows.push(lane);
    trace.branches[letter] = { name: '分支 ' + letter + (prompt ? ' · ' + firstLine(contentText(prompt.message.content), 30) : ''), variant: projected.session.model ?? 'Pi' };
    trace.result[letter] = summarize(steps, 1).result.M;
    trace.steps.push(...steps);
  });
  trace.steps.sort((a, b) => a.t0 - b.t0 || a.id.localeCompare(b.id));
  return trace;
}
