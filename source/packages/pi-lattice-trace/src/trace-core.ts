// Shared step model for the Lattice Trace viewer (kernelbridge.trace/v1).
// Source-specific projections (KernelBridge Journal, Pi session entries) build
// steps with these helpers; nothing here reads files or knows the data source.

export const TRACE_SCHEMA = 'kernelbridge.trace/v1';
export const MAIN = 'M/main';
export const OUT_LIMIT = 4000;

export type Json = any;
export type Step = Record<string, Json>;
/** Page text that differs between hosts; the adapter falls back to the KernelBridge wording. */
export interface TraceLabels { harness?: string; eyebrow?: string; source?: string; defaultTitle?: string; workspace?: string; filesNote?: string; childNote?: string }
export interface Trace {
  schema: string; seq: number; live: boolean;
  session: { id: string | null; title: string; model: string | null; cwd: string | null; started_ns: number | null; updated_ns: number | null; status: string };
  limit: number; thresh: number; budget: number; config: Json;
  agents: Record<string, Json>; rows: string[]; branches: Record<string, Json>;
  steps: Step[]; commits: Json[]; files: Record<string, [string, string][]>; result: Record<string, Json>;
  labels?: TraceLabels;
}

export const round2 = (value: number) => Math.round(value * 100) / 100;
export const text = (value: unknown) => typeof value === 'string' ? value : value === undefined || value === null ? '' : JSON.stringify(value);
export const clip = (value: string, limit = OUT_LIMIT) => value.length > limit ? value.slice(0, limit) + `\n…（截断，共 ${value.length} 字符）` : value;
/** Clipped output plus explicit truncation fields, so a clipped value is never mistaken for the whole. */
export const clipped = (value: string, limit = OUT_LIMIT): { out: string; truncated?: true; originalLength?: number } =>
  value.length > limit ? { out: clip(value, limit), truncated: true, originalLength: value.length } : { out: value };
export const firstLine = (value: string, limit = 60) => { const line = value.trim().split('\n').find(Boolean) ?? ''; return line.length > limit ? line.slice(0, limit - 1) + '…' : line; };
export const tokens = (value: string) => Math.max(1, Math.ceil(value.length / 4));

/** Text of a content array (text and thinking blocks), or the value itself. */
export function contentText(content: Json): string {
  if (!Array.isArray(content)) return text(content);
  return content.map(block => block?.type === 'text' ? block.text : block?.type === 'thinking' ? block.thinking ?? block.text ?? '' : '').filter(Boolean).join('\n');
}

export function toolTitle(tool: string, args: Json): string {
  const a = args && typeof args === 'object' ? args : {};
  const path = a.path ?? a.file ?? a.file_path;
  if (tool === 'read' || tool === 'read_file') return '读取 ' + (path ?? '');
  if (tool === 'write' || tool === 'write_file') return '写入 ' + (path ?? '');
  if (tool === 'edit') return '修改 ' + (path ?? '');
  if (tool === 'bash' || tool === 'run_command') return '运行 ' + firstLine(text(a.command ?? a.cmd ?? ''), 48);
  if (tool === 'grep') return '搜索 ' + firstLine(text(a.pattern ?? ''), 40);
  if (tool === 'find' || tool === 'ls') return '浏览 ' + (a.path ?? a.pattern ?? '.');
  if (tool === 'delegate') return '委派 ' + (a.agent ?? '子代理');
  if (tool === 'update_plan' && Array.isArray(a.plan)) return `更新计划 ${a.plan.filter((item: Json) => item?.status === 'completed').length}/${a.plan.length}`;
  return tool;
}

// File contents rebuilt from this session's own write/edit calls: [line, commit] pairs.
export function applyWrite(files: Record<string, [string, string][]>, path: string, content: string, commit: string) {
  const del = files[path]?.length ?? 0;
  files[path] = content.split('\n').map(line => [line, commit]);
  return { add: files[path].length, del };
}

export function applyEdit(files: Record<string, [string, string][]>, path: string, args: Json, commit: string) {
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

/** Records a successful write/edit as a commit and rebuilds the file for the blame view. */
export function recordFileChange(step: Step, tool: string, args: Json, files: Record<string, [string, string][]>, commits: Json[]) {
  const path = args?.path ?? args?.file_path;
  if (!['write', 'edit', 'write_file'].includes(tool) || typeof path !== 'string') return;
  const commit = 'E' + String(commits.length + 1).padStart(2, '0');
  const change = tool === 'edit' ? applyEdit(files, path, args, commit) : applyWrite(files, path, text(args.content), commit);
  step.commit = commit;
  commits.push({ id: commit, step: step.id, commitStep: step.id, agent: step.agent, branch: 'M', t: step.t1, files: [{ path, ...change }] });
}

// Tool outputs and inputs are sized by estimate; scale each gap so that the
// context the viewer rebuilds matches the provider's prompt tokens exactly.
export function calibrate(steps: Step[]) {
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

/** Sorts, calibrates and numbers tool calls within each model turn, as the viewer's own builders do. */
export function finishSteps(steps: Step[]) {
  steps.sort((a, b) => a.t0 - b.t0 || a.id.localeCompare(b.id));
  calibrate(steps);
  const count: Record<string, number> = {};
  for (const step of steps) if (step.src === 'tool') { const key = step.turn ?? step.id; step.stack = count[key] ?? 0; count[key] = step.stack + 1; }
  for (const step of steps) if (step.src === 'model') step.nTools = count[step.id] ?? 0;
}

/** Session status and the per-branch result card. */
export function summarize(steps: Step[], lanes: number) {
  const pending = steps.some(step => step.status === 'running');
  const status = pending ? 'running' : steps.length ? 'idle' : 'empty';
  const failures = steps.filter(step => step.status === 'error').length;
  const end = steps.reduce((value, step) => Math.max(value, step.t1), 0);
  const result = { M: { ok: failures === 0, label: pending ? '进行中' : failures ? `${failures} 处失败` : '正常', note: `${steps.length} 步 · ${lanes - 1} 个子代理`, endLabel: pending ? '进行中' : (end / 60).toFixed(1) + ' min' } };
  return { status, result };
}
