// Read-only projection of a KernelBridge Journal into the Lattice Trace step
// model (kernelbridge.trace/v1). The Journal stays the only authority: this
// module never writes and tolerates a partially written tail.

export const TRACE_SCHEMA = 'kernelbridge.trace/v1';
const MAIN = 'M/main';
const OUT_LIMIT = 4000;

type Json = any;
export type Step = Record<string, Json>;
export interface Trace {
  schema: string; seq: number; live: boolean;
  session: { id: string | null; title: string; model: string | null; cwd: string | null; started_ns: number | null; updated_ns: number | null; status: string };
  limit: number; thresh: number; budget: number; config: Json;
  agents: Record<string, Json>; rows: string[]; branches: Record<string, Json>;
  steps: Step[]; commits: Json[]; files: Record<string, [string, string][]>; result: Record<string, Json>;
}

// Tool outputs and inputs are sized by estimate; scale each gap so that the
// context the viewer rebuilds matches the provider's prompt_tokens exactly.
function calibrate(steps: Step[]) {
  const lanes = new Map<string, Step[]>();
  for (const step of steps) { const lane = lanes.get(step.agent) ?? []; lane.push(step); lanes.set(step.agent, lane); }
  for (const lane of lanes.values()) {
    let end: number | null = null; let between: Step[] = [];
    const size = (step: Step) => step.src === 'tool' ? step.outTok ?? 0 : step.addTok ?? 0;
    for (const step of lane) {
      if (step.src === 'model' && step.status !== 'running') {
        const estimate = between.reduce((sum, item) => sum + size(item), 0); const target = end === null ? 0 : step.tokIn - end;
        if (end !== null && estimate > 0 && target > 0) {
          let left = target;
          between.forEach((item, index) => {
            const value = index === between.length - 1 ? Math.max(1, left) : Math.max(1, Math.round(size(item) * target / estimate));
            left -= value; if (item.src === 'tool') item.outTok = value; else item.addTok = value;
          });
        }
        end = step.tokIn + step.tokOut; between = [];
      } else if (step.src === 'harness' && step.kind === 'compact') { end = step.after; between = []; }
      else if (step.src === 'tool' || step.addTok) between.push(step);
    }
  }
  const running = new Map<string, number>();
  for (const step of steps) {
    const prior = running.get(step.agent) ?? 0;
    if (step.src === 'model') step.ctx = step.tokIn + step.tokOut;
    else if (step.src === 'tool') step.ctx = prior + (step.outTok ?? 0);
    else if (step.kind === 'compact') step.ctx = step.after;
    else step.ctx = prior + (step.addTok ?? 0);
    running.set(step.agent, step.ctx);
  }
}

// File contents rebuilt from this session's own write/edit calls: [line, commit] pairs.
function applyWrite(files: Record<string, [string, string][]>, path: string, content: string, commit: string) {
  const del = files[path]?.length ?? 0;
  files[path] = content.split('\n').map(line => [line, commit]);
  return { add: files[path].length, del };
}

function applyEdit(files: Record<string, [string, string][]>, path: string, args: Json, commit: string) {
  let edits: Json = args?.edits ?? [{ oldText: args?.oldText, newText: args?.newText }];
  if (typeof edits === 'string') { try { edits = JSON.parse(edits); } catch { edits = []; } }
  if (!Array.isArray(edits)) edits = [edits];
  let lines: [string, string][] = files[path] ?? [['…（修改前的内容不在本会话中）', '—']];
  let add = 0, del = 0;
  for (const edit of edits) {
    if (typeof edit?.oldText !== 'string' || typeof edit?.newText !== 'string') continue;
    const body = lines.map(line => line[0]).join('\n'); const index = body.indexOf(edit.oldText);
    const fresh = edit.newText.split('\n').map((line: string): [string, string] => [line, commit]);
    add += fresh.length; del += edit.oldText.split('\n').length;
    if (index < 0 || !edit.oldText) { lines = [...lines, ['…', '—'], ...fresh]; continue; }
    const first = body.slice(0, index).split('\n').length - 1; const last = first + edit.oldText.split('\n').length - 1;
    const head = body.slice(0, index).split('\n').pop() ?? ''; const tail = body.slice(index + edit.oldText.length).split('\n')[0] ?? '';
    fresh[0][0] = head + fresh[0][0]; fresh[fresh.length - 1][0] += tail;
    lines = [...lines.slice(0, first), ...fresh, ...lines.slice(last + 1)];
  }
  files[path] = lines;
  return { add, del };
}

interface ActionRecord { id: string; tool: string; task: string; args: Json; parents: string[]; admitted: number; started?: number; settled?: number; outcome?: Json; elapsed?: number; gate?: Json }

const round2 = (value: number) => Math.round(value * 100) / 100;
const text = (value: unknown) => typeof value === 'string' ? value : value === undefined || value === null ? '' : JSON.stringify(value);
const clip = (value: string, limit = OUT_LIMIT) => value.length > limit ? value.slice(0, limit) + `\n…（截断，共 ${value.length} 字符）` : value;
const firstLine = (value: string, limit = 60) => { const line = value.trim().split('\n').find(Boolean) ?? ''; return line.length > limit ? line.slice(0, limit - 1) + '…' : line; };
const tokens = (value: string) => Math.max(1, Math.ceil(value.length / 4));

function contentText(content: Json): string {
  if (!Array.isArray(content)) return text(content);
  return content.map(block => block?.type === 'text' ? block.text : block?.type === 'thinking' ? block.thinking ?? block.text ?? '' : '').filter(Boolean).join('\n');
}

function resultText(outcome: Json): string {
  const data = outcome?.data;
  if (outcome?.status === 'failed') return text(data?.error ?? data?.message ?? data ?? 'failed');
  if (data && typeof data === 'object' && data.result && typeof data.result === 'object') {
    const content = contentText(data.result.content);
    return content || text(data.result.structuredContent ?? data.result);
  }
  if (data && typeof data === 'object' && typeof data.summary === 'string') return data.summary;
  return text(data);
}

function toolTitle(tool: string, args: Json): string {
  const a = args && typeof args === 'object' ? args : {};
  const path = a.path ?? a.file ?? a.file_path;
  if (tool === 'read' || tool === 'read_file') return '读取 ' + (path ?? '');
  if (tool === 'write' || tool === 'write_file') return '写入 ' + (path ?? '');
  if (tool === 'edit') return '修改 ' + (path ?? '');
  if (tool === 'bash' || tool === 'run_command') return '运行 ' + firstLine(text(a.command ?? a.cmd ?? ''), 48);
  if (tool === 'grep') return '搜索 ' + firstLine(text(a.pattern ?? ''), 40);
  if (tool === 'find' || tool === 'ls') return '浏览 ' + (a.path ?? a.pattern ?? '.');
  if (tool === 'delegate') return '委派 ' + (a.agent ?? '子代理');
  return tool;
}

/** Parse complete JSONL rows; an unterminated final line is left for the next read. */
export function parseRows(buffer: string): { rows: Json[]; rest: string } {
  const end = buffer.lastIndexOf('\n');
  if (end < 0) return { rows: [], rest: buffer };
  const rows: Json[] = [];
  for (const line of buffer.slice(0, end).split('\n')) {
    if (!line.trim()) continue;
    try { rows.push(JSON.parse(line)); } catch { /* skip a corrupt row; the Journal verifier owns integrity */ }
  }
  return { rows, rest: buffer.slice(end + 1) };
}

export function projectTrace(rows: Json[], options: { now_ns?: number; live?: boolean; title?: string } = {}): Trace {
  const now = options.now_ns ?? Date.now() * 1e6;
  // Executor rows carry no clock; bound each by the neighbouring application rows.
  const known: (number | undefined)[] = rows.map(row => typeof row.time_ns === 'number' ? row.time_ns : undefined);
  const before: number[] = []; const after: number[] = [];
  let last: number | undefined;
  for (let i = 0; i < rows.length; i++) { if (known[i] !== undefined) last = known[i]; before[i] = last ?? NaN; }
  last = undefined;
  for (let i = rows.length - 1; i >= 0; i--) { if (known[i] !== undefined) last = known[i]; after[i] = last ?? NaN; }
  const start = known.find(value => value !== undefined) ?? now;
  const at = (index: number | undefined, side: 'before' | 'after' = 'before') => {
    if (index === undefined) return (now - start) / 1e9;
    const value = side === 'before' ? (Number.isNaN(before[index]) ? after[index] : before[index]) : (Number.isNaN(after[index]) ? now : after[index]);
    return Math.max(0, ((Number.isNaN(value) ? now : value) - start) / 1e9);
  };

  let config: Json = {};
  let mainTask: string | null = null; let model: string | null = null; let limit = 128000; let budget = 0;
  let title = options.title ?? ''; let named = ''; let objective = ''; let firstPrompt = ''; let cwd: string | null = null; let session: string | null = null; let status = 'idle';
  const actions = new Map<string, ActionRecord>(); const order: string[] = [];
  const inputs: { index: number; data: Json }[] = []; const transitions: { index: number; data: Json }[] = [];
  let seq = 0; let updated: number | null = null;
  rows.forEach((row, index) => {
    seq = Math.max(seq, row.seq ?? 0);
    if (typeof row.time_ns === 'number') updated = row.time_ns;
    if (row.application_session_id) session ??= row.application_session_id;
    const kind = row.kind;
    if (kind === 'task' && !mainTask) { mainTask = row.task?.id ?? null; title ||= ''; }
    else if (kind === 'admitted' && row.action) {
      const action = row.action;
      if (!actions.has(action.id)) order.push(action.id);
      actions.set(action.id, { id: action.id, tool: action.tool, task: action.task, args: action.arguments, parents: action.parents ?? [], admitted: index });
    } else if (kind === 'started') { const a = actions.get(row.action_id); if (a) a.started = index; }
    else if (kind === 'settled') { const a = actions.get(row.action_id); if (a) { a.settled = index; a.outcome = row.outcome; a.elapsed = row.elapsed_ms; } }
    else if (kind === 'application/call_result' && row.data?.result?.model_info) {
      const info = row.data.result.model_info; model = info.name ?? model; limit = info.context_window ?? limit;
      const harness = row.data.result.native_harness ?? {};
      cwd = harness.workspace?.root ?? cwd;
      config = { adapter: harness.adapter ?? null, workspace: harness.workspace?.mode ?? null, tools: [...(harness.native_tools ?? []), ...(row.data.result.project_tools ?? [])],
        execution: row.data.result.execution_mode ?? null };
    } else if (kind === 'application/input_accepted') {
      inputs.push({ index, data: row.data });
      if (!firstPrompt && row.data?.kind === 'prompt' && row.data?.initiator === 'user') firstPrompt = text(row.data.text);
    }
    else if (kind === 'application/configuration') {
      const op = row.data?.operation;
      if (op === 'rename' && row.data.name) named = row.data.name;
      if (op === 'transition_prepared') transitions.push({ index, data: row.data });
    } else if (kind === 'application/created') {
      objective = row.data?.goal?.objective ?? objective;
    }
  });

  // Agents: the main task plus one lane per delegated child (task "<delegate action>/leaf").
  const agents: Record<string, Json> = { [MAIN]: { branch: 'M', name: '主会话', short: '主会话', tag: 'main', level: 0, sb: '—', ws: '—' } };
  const rowsOrder = [MAIN]; const laneOf = new Map<string, string>(); const childOf = new Map<string, string>();
  const lane = (task: string): string => {
    if (!mainTask || task === mainTask) return MAIN;
    const known = laneOf.get(task); if (known) return known;
    const delegate = actions.get(task.replace(/\/leaf$/, ''));
    const key = `M/c${rowsOrder.length}`; const agent = delegate?.args?.agent ?? '子代理';
    agents[key] = { branch: 'M', name: '子代理 · ' + agent, short: agent, tag: agent, level: 1, parent: MAIN, sb: '—', ws: '—',
      ro: (delegate?.args?.mode ?? 'read_only') === 'read_only', task: delegate?.args?.task ?? '' };
    rowsOrder.push(key); laneOf.set(task, key); if (delegate) childOf.set(delegate.id, key);
    return key;
  };
  for (const id of order) { const a = actions.get(id)!; if (a.task !== mainTask && a.task.endsWith('/leaf')) lane(a.task); }

  const steps: Step[] = []; const commits: Json[] = []; const files: Record<string, [string, string][]> = {}; const ctx = new Map<string, number>(); const turns = new Map<string, number>();
  const modelStep = new Map<string, string>(); const gates = new Map<string, Json>();
  // A gate's id is "<tool action>/gate/<policy digest>".
  for (const id of order) { const a = actions.get(id)!; if (a.tool === '__gate__' && a.outcome) gates.set(a.id.replace(/\/gate\/[^/]+$/, ''), a.outcome.data); }
  // Ids derive from Journal positions so they stay stable while the session grows.
  const sid = (prefix: string, index: number) => prefix + String(rows[index]?.seq ?? index).padStart(5, '0');
  const times = (a: ActionRecord) => {
    const t0 = at(a.started ?? a.admitted, 'before');
    if (a.settled === undefined) return { t0: round2(t0), t1: round2(Math.max(t0 + 0.05, at(undefined))), running: true };
    const bound = at(a.settled, 'after');
    const t1 = a.elapsed !== undefined ? Math.min(t0 + a.elapsed / 1000, Math.max(bound, t0)) : bound;
    return { t0: round2(t0), t1: round2(Math.max(t1, t0 + 0.01)), running: false };
  };

  for (const id of order) {
    const a = actions.get(id)!; const agent = lane(a.task); const span = times(a);
    const failed = a.outcome?.status === 'failed';
    const st = span.running ? 'running' : failed ? 'error' : 'ok';
    if (a.tool === '__propose__') {
      const data = a.outcome?.data ?? {}; const usage = data.usage ?? {};
      const said = contentText(data.content); const calls = Array.isArray(data.content) ? data.content.filter((b: Json) => b?.type === 'toolCall') : [];
      const prior = ctx.get(agent) ?? 0;
      if (/\/compact\//.test(a.id)) {
        const after = usage.completion_tokens ?? tokens(said); const before = usage.prompt_tokens ?? prior;
        steps.push({ id: sid('K', a.admitted), agent, branch: 'M', src: 'harness', kind: 'compact', t0: span.t0, t1: span.t1, title: `上下文压缩 ${(before / 1000).toFixed(1)}k → ${(after / 1000).toFixed(1)}k`,
          status: span.running ? 'running' : 'warn', note: clip(said || '压缩中…'), before, after, ctx: after });
        ctx.set(agent, after); continue;
      }
      const turnNo = (turns.get(agent) ?? 0) + 1; turns.set(agent, turnNo);
      const tokIn = usage.prompt_tokens ?? prior; const tokOut = usage.completion_tokens ?? (said ? tokens(said) : 0);
      const names = calls.map((c: Json) => c.name);
      const step: Step = { id: sid('T', a.admitted), agent, branch: 'M', src: 'model', kind: !span.running && !calls.length && !failed ? 'final' : 'think', t0: span.t0, t1: span.t1,
        title: span.running ? '生成中…' : failed ? '模型调用失败' : firstLine(said) || (names.length ? '调用 ' + [...new Set(names)].join('、') : '思考'),
        status: st, tokIn, tokOut, cacheRead: 0, cacheReal: usage.prompt_cache_hit_tokens ?? usage.prompt_tokens_details?.cached_tokens ?? usage.cached_tokens ?? usage.cache_read_tokens ?? null,
        turnNo, thought: clip(failed ? resultText(a.outcome) : said), ctx: tokIn + tokOut, action_id: a.id };
      steps.push(step); modelStep.set(a.id, step.id); ctx.set(agent, tokIn + tokOut);
      continue;
    }
    if (a.tool.startsWith('__')) continue;
    const turn = modelStep.get(a.parents[0] ?? ''); const denied = gates.get(a.id)?.admit === false;
    const out = span.running ? '' : clip(resultText(a.outcome));
    const isError = a.outcome?.data?.result?.isError === true;
    const outTok = out ? tokens(out) : 0; const prior = ctx.get(agent) ?? 0;
    const base: Step = { id: sid('T', a.admitted), agent, branch: 'M', src: 'tool', kind: 'call', t0: span.t0, t1: span.t1, title: toolTitle(a.tool, a.args),
      status: denied ? 'denied' : isError ? 'error' : st, tool: a.tool, args: a.args ?? {}, out, outTok, turn, ctx: prior + outTok, action_id: a.id };
    if (a.tool === 'delegate') {
      const child = childOf.get(a.id);
      const spawnEnd = round2(Math.min(span.t1, span.t0 + 0.3));
      steps.push({ ...base, kind: 'spawn', child, t1: spawnEnd, out: child ? '已派生 ' + agents[child].name : out || '未启动', outTok: child ? 40 : outTok,
        status: child ? (span.running ? 'running' : 'ok') : base.status });
      if (child && !span.running) steps.push({ ...base, id: sid('R', a.settled!), kind: 'return', child, t0: span.t1, t1: round2(span.t1 + 0.05), title: '子代理返回：' + agents[child].short,
        status: failed ? 'error' : 'ok', out, outTok, turn: undefined });
      ctx.set(agent, prior + (child ? 40 + (span.running ? 0 : outTok) : outTok));
      continue;
    }
    steps.push(base); ctx.set(agent, prior + outTok);
    const path = a.args?.path ?? a.args?.file_path;
    if (!span.running && !failed && !isError && ['write', 'edit', 'write_file'].includes(a.tool) && typeof path === 'string') {
      const commit = 'E' + String(commits.length + 1).padStart(2, '0');
      const change = a.tool === 'edit' ? applyEdit(files, path, a.args, commit) : applyWrite(files, path, text(a.args.content), commit);
      base.commit = commit;
      commits.push({ id: commit, step: base.id, commitStep: base.id, agent, branch: 'M', t: base.t1, files: [{ path, ...change }] });
    }
  }

  for (const { index, data } of inputs) {
    const value = text(data.text); const t = round2(at(index));
    if (data.kind === 'control' && value.startsWith('{')) continue;
    const goal = data.initiator === 'loopx' || data.channel === 'policy' && data.initiator !== 'agent';
    const notice = data.initiator === 'agent';
    steps.push({ id: sid('U', index), agent: MAIN, branch: 'M', src: 'harness', kind: goal ? 'goal' : 'input', t0: t, t1: round2(t + 0.05),
      title: goal ? 'Goal 续跑' : notice ? '子代理结果通知' : data.kind === 'control' ? '命令 ' + firstLine(value, 40) : '用户：' + firstLine(value, 40),
      status: 'ok', note: clip(value, 2000), ctx: 0, addTok: data.kind === 'control' ? 0 : tokens(value) });
  }
  for (const { index, data } of transitions) {
    const op = data.parent?.operation; const t = round2(at(index));
    steps.push({ id: sid('F', index), agent: MAIN, branch: 'M', src: 'harness', kind: op === 'fork' ? 'fork' : 'checkpoint', t0: t, t1: t,
      title: op === 'fork' ? '/fork 新分支' : op === 'reset' ? '/new 新上下文' : '上下文切换', status: 'ok',
      note: op === 'fork' ? '对话从这里 fork 成新的 Pi 会话；原分支保留。' : '新的对话上下文；之前的历史不再进入模型。', ctx: 0 });
  }
  steps.sort((a, b) => a.t0 - b.t0 || a.id.localeCompare(b.id));
  calibrate(steps);
  // Stack index of tool calls within one model turn (as the viewer's own builders do).
  const count: Record<string, number> = {};
  for (const step of steps) if (step.src === 'tool') { const key = step.turn ?? step.id; step.stack = count[key] ?? 0; count[key] = step.stack + 1; }
  for (const step of steps) if (step.src === 'model') step.nTools = count[step.id] ?? 0;

  const pending = steps.some(step => step.status === 'running');
  status = pending ? 'running' : steps.length ? 'idle' : 'empty';
  const failures = steps.filter(step => step.status === 'error').length;
  const end = steps.reduce((value, step) => Math.max(value, step.t1), 0);
  return {
    schema: TRACE_SCHEMA, seq, live: options.live ?? false,
    session: { id: session, title: firstLine(named || title || objective || firstPrompt, 120) || 'KernelBridge 会话', model, cwd, started_ns: known.find(v => v !== undefined) ?? null, updated_ns: updated, status },
    limit, thresh: Math.round(limit * 0.8), budget, config,
    agents, rows: rowsOrder, branches: { M: { name: '当前会话', variant: model ?? 'KernelBridge' } },
    steps, commits, files,
    result: { M: { ok: failures === 0, label: pending ? '进行中' : failures ? `${failures} 处失败` : '正常', note: `${steps.length} 步 · ${rowsOrder.length - 1} 个子代理`, endLabel: pending ? '进行中' : (end / 60).toFixed(1) + ' min' } },
  };
}
