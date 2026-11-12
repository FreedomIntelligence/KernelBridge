// Live trace: Journal projection, viewer assembly and the local SSE server.
import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, writeFile, appendFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { request } from 'node:http';
import { parseRows, projectTrace } from '../../source/apps/agent/src/trace/journal-trace.ts';
import { startTraceServer, viewerPage } from '../../source/apps/agent/src/trace/server.ts';

const TASK = 'codegen-test';
const S = 1_800_000_000_000_000_000;
/** A small Journal: two prompts, a read, a delegated child, an edit, a compaction and a fork. */
export function journal() {
  const rows = []; let seq = 0; let clock = S;
  const add = row => { rows.push({ seq: seq++, ...row }); };
  const app = (kind, data) => { clock += 200_000_000; add({ kind, application_session_id: 'application-test', data, time_ns: clock }); };
  const action = (id, tool, args, outcome, { task = TASK, parents = [], call = 'c' + seq, elapsed = 150 } = {}) => {
    app('application/call_intent', { call_id: call, method: tool === '__propose__' ? 'model' : 'tool' });
    add({ kind: 'admitted', action: { id, tool, task, arguments: args, parents }, application_context: { call_id: call } });
    add({ kind: 'started', action_id: id });
    if (outcome) { add({ kind: 'settled', action_id: id, elapsed_ms: elapsed, outcome }); app('application/call_result', { call_id: call, result: {} }); }
  };
  const turn = (text, calls, usage) => ({ status: 'completed', data: { content: [...(text ? [{ type: 'text', text }] : []), ...calls.map((c, i) => ({ type: 'toolCall', id: 'k' + i, name: c }))], stop_reason: calls.length ? 'tool_calls' : 'stop', usage } });
  add({ kind: 'task', task: { id: TASK, goal: 'Work on the user’s explicit coding requests in this project.' } });
  app('application/created', { task: { goal: 'generic' } });
  app('application/call_result', { call_id: 'hello', result: { model_info: { name: 'fixture-model', context_window: 64000 }, execution_mode: 'sequential',
    native_harness: { adapter: 'pi@1.5.0', native_tools: ['read', 'edit'], workspace: { mode: 'project', root: '/work/project' } } } });
  app('application/input_accepted', { kind: 'prompt', initiator: 'user', channel: 'tui', text: 'Read solution.py and fix x' });
  const m1 = TASK + '/step/0/model/primary';
  action(m1, '__propose__', {}, turn('Reading the file first.', ['read'], { prompt_tokens: 3000, completion_tokens: 40, prompt_cache_hit_tokens: 0 }));
  action(m1 + '/call/0', 'read', { path: 'solution.py' }, { status: 'completed', data: { result: { content: [{ type: 'text', text: 'x = 1\ny = 2\n' }], isError: false } } }, { parents: [m1] });
  const m2 = TASK + '/step/1/model/primary';
  action(m2, '__propose__', {}, turn('', ['delegate', 'edit'], { prompt_tokens: 3200, completion_tokens: 60, prompt_cache_hit_tokens: 2944 }));
  const d = m2 + '/call/0';
  action(d, 'delegate', { agent: 'kernel-scout', task: 'Find callers of x', mode: 'read_only' }, null, { parents: [m2] });
  const leaf = d + '/leaf';
  add({ kind: 'task', task: { id: leaf, goal: 'Find callers of x' } });
  action(leaf + '/step/0/model/primary', '__propose__', {}, turn('Searching.', ['read_file'], { prompt_tokens: 1500, completion_tokens: 20 }), { task: leaf });
  action(leaf + '/step/0/model/primary/call/0', 'read_file', { path: 'main.py' }, { status: 'completed', data: { result: { content: [{ type: 'text', text: 'print(x)' }] } } }, { task: leaf, parents: [leaf + '/step/0/model/primary'] });
  action(leaf + '/step/1/model/primary', '__propose__', {}, turn('main.py prints x.', [], { prompt_tokens: 1560, completion_tokens: 12 }), { task: leaf });
  add({ kind: 'settled', action_id: d, elapsed_ms: 2000, outcome: { status: 'completed', data: { summary: 'main.py prints x.' } } });
  action(m2 + '/call/1', 'edit', { path: 'solution.py', edits: [{ oldText: 'x = 1', newText: 'x = 3' }] }, { status: 'completed', data: { result: { content: [{ type: 'text', text: 'ok' }] } } }, { parents: [m2] });
  action(TASK + '/compact/u1', '__propose__', {}, turn('Summary: x fixed in solution.py', [], { prompt_tokens: 3500, completion_tokens: 300 }));
  app('application/configuration', { operation: 'transition_prepared', parent: { operation: 'fork' } });
  app('application/configuration', { operation: 'rename', name: 'Fix x' });
  app('application/input_accepted', { kind: 'prompt', initiator: 'user', channel: 'tui', text: 'Thanks' });
  action(TASK + '/step/2/model/primary', '__propose__', {}, null);
  return rows.map(row => JSON.stringify(row)).join('\n') + '\n';
}

test('projects model, tool, child, edit, compaction and fork rows', () => {
  const { rows, rest } = parseRows(journal());
  assert.equal(rest, '');
  const trace = projectTrace(rows, { now_ns: S + 30e9 });
  assert.equal(trace.schema, 'kernelbridge.trace/v1');
  assert.equal(trace.session.title, 'Fix x');
  assert.equal(trace.session.model, 'fixture-model');
  assert.equal(trace.session.status, 'running');
  assert.equal(trace.limit, 64000);
  assert.deepEqual(trace.rows, ['M/main', 'M/c1']);
  assert.equal(trace.agents['M/c1'].short, 'kernel-scout');
  assert.equal(trace.agents['M/c1'].ro, true);
  const kinds = trace.steps.map(s => `${s.agent}:${s.src}:${s.kind}`);
  for (const kind of ['M/main:harness:input', 'M/main:model:think', 'M/main:tool:call', 'M/main:tool:spawn', 'M/main:tool:return', 'M/c1:model:think', 'M/c1:tool:call', 'M/c1:model:final', 'M/main:harness:compact', 'M/main:harness:fork'])
    assert.ok(kinds.includes(kind), kind);
  const read = trace.steps.find(s => s.tool === 'read');
  assert.equal(read.title, '读取 solution.py'); assert.match(read.out, /x = 1/);
  assert.equal(read.turn, trace.steps.find(s => s.src === 'model').id);
  const spawn = trace.steps.find(s => s.kind === 'spawn'); const back = trace.steps.find(s => s.kind === 'return');
  assert.equal(spawn.child, 'M/c1'); assert.equal(back.out, 'main.py prints x.'); assert.ok(back.t0 >= spawn.t0);
  const cached = trace.steps.filter(s => s.src === 'model' && s.agent === 'M/main');
  assert.equal(cached[1].cacheReal, 2944);
  const live = trace.steps.at(-1); assert.equal(live.status, 'running'); assert.equal(live.title, '生成中…');
  assert.ok(live.t1 > live.t0);
  // Edits rebuild the file from this session's own calls.
  assert.equal(trace.commits.length, 1);
  assert.deepEqual(trace.commits[0].files, [{ path: 'solution.py', add: 1, del: 1 }]);
  assert.equal(trace.files['solution.py'].at(-1)[0], 'x = 3');
  assert.equal(trace.result.M.label, '进行中');
});

test('tool and input sizes are calibrated to the provider prompt tokens', () => {
  const trace = projectTrace(parseRows(journal()).rows, { now_ns: S + 30e9 });
  const main = trace.steps.filter(s => s.agent === 'M/main');
  const models = main.filter(s => s.src === 'model' && s.status !== 'running');
  const [first, second] = models;
  const between = main.filter(s => s.t0 >= first.t0 && s.t0 < second.t0 && (s.src === 'tool' || s.addTok));
  const added = between.reduce((sum, s) => sum + (s.src === 'tool' ? s.outTok : s.addTok), 0);
  assert.equal(first.tokIn + first.tokOut + added, second.tokIn);
});

test('ids are stable while the Journal grows and torn tails wait for the newline', () => {
  const text = journal(); const cut = text.indexOf('\n', text.length / 2);
  const partial = parseRows(text.slice(0, cut + 20));
  assert.ok(partial.rest.length > 0, 'unterminated line is kept for the next read');
  const early = projectTrace(partial.rows, { now_ns: S + 30e9 });
  const full = projectTrace(parseRows(text).rows, { now_ns: S + 30e9 });
  const ids = new Set(full.steps.map(s => s.id));
  for (const step of early.steps.filter(s => s.status !== 'running' && s.kind !== 'spawn')) assert.ok(ids.has(step.id), step.id);
});

test('the vendored viewer accepts every live transform exactly once', async () => {
  const trace = projectTrace(parseRows(journal()).rows);
  trace.session.title = '</script><script>alert(1)</script>';
  const page = await viewerPage(trace);
  assert.match(page, /<title>KernelBridge · 实时轨迹<\/title>/);
  assert.ok(page.indexOf('id="kb-trace"') < page.indexOf('/* kernelbridge live adapter */'));
  assert.ok(page.indexOf('/* kernelbridge live adapter */') < page.indexOf('\nloadSession(DS_INV);\nreadHash();\n'));
  assert.ok(!page.includes('</script><script>alert(1)'), 'embedded JSON cannot close the script element');
  assert.match(page, /if \(DS\.live\) L = DS\.baseCtx/);
  assert.match(page, /DS\.live \? DS\.configHTML\(\)/);
});

function get(port, path, headers = {}) {
  return new Promise((accept, reject) => {
    const req = request({ host: '127.0.0.1', port, path, headers }, response => {
      let body = ''; response.setEncoding('utf8');
      response.on('data', chunk => { body += chunk; });
      response.on('end', () => accept({ status: response.statusCode, body, type: response.headers['content-type'] }));
    });
    req.on('error', reject); req.end();
  });
}

test('server requires the token and loopback host, and streams Journal growth', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'kb-trace-'));
  const path = join(directory, 'events.jsonl'); const text = journal(); const cut = text.indexOf('\n', text.length / 2) + 1;
  await writeFile(path, text.slice(0, cut));
  const server = await startTraceServer({ journal: path });
  try {
    const url = new URL(server.url); const base = url.pathname;
    assert.match(base, /^\/[0-9a-f]{32}\/$/);
    assert.equal((await get(server.port, '/')).status, 404);
    assert.equal((await get(server.port, '/' + '0'.repeat(32) + '/')).status, 404);
    assert.equal((await get(server.port, base, { host: 'evil.example:' + server.port })).status, 404);
    const page = await get(server.port, base);
    assert.equal(page.status, 200); assert.match(page.type, /text\/html/); assert.match(page.body, /kernelbridge live adapter/);
    const before = JSON.parse((await get(server.port, base + 'trace.json')).body);
    const events = [];
    const stream = await new Promise((accept, reject) => {
      const req = request({ host: '127.0.0.1', port: server.port, path: base + 'events' }, response => {
        response.setEncoding('utf8'); let buffer = '';
        response.on('data', chunk => {
          buffer += chunk; let end;
          while ((end = buffer.indexOf('\n\n')) >= 0) {
            const frame = buffer.slice(0, end); buffer = buffer.slice(end + 2);
            const data = frame.split('\n').find(line => line.startsWith('data: '));
            if (frame.includes('event: trace') && data) events.push(JSON.parse(data.slice(6)));
          }
        });
        accept(req);
      });
      req.on('error', reject); req.end();
    });
    const wait = async predicate => { for (let i = 0; i < 100 && !predicate(); i++) await new Promise(r => setTimeout(r, 50)); assert.ok(predicate()); };
    await wait(() => events.length >= 1);
    assert.equal(events[0].steps.length, before.steps.length);
    await appendFile(path, text.slice(cut));
    await wait(() => events.some(trace => trace.steps.length > before.steps.length));
    assert.deepEqual(events.at(-1).rows, ['M/main', 'M/c1']);
    stream.destroy();
  } finally { await server.close(); await rm(directory, { recursive: true, force: true }); }
});
