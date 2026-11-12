// pi-lattice-trace: projection of Pi session entries, page assembly and the
// shared server's source switching. Entries follow Pi 1.5.0's session format.
import test from 'node:test';
import assert from 'node:assert/strict';
import { request } from 'node:http';
import { projectSession, PI_LABELS } from '../../source/packages/pi-lattice-trace/src/session-trace.ts';
import { viewerPage } from '../../source/packages/pi-lattice-trace/src/viewer-page.ts';
import { serveTrace } from '../../source/packages/pi-lattice-trace/src/server.ts';

const T0 = Date.parse('2026-11-12T04:00:00.000Z');
const iso = ms => new Date(T0 + ms).toISOString();
const header = { type: 'session', version: 3, id: 'sess-1', timestamp: iso(0), cwd: '/work/project' };
const usage = (input, output, cacheRead = 0) => ({ input, output, cacheRead, cacheWrite: 0, totalTokens: input + output + cacheRead, cost: { total: 0 } });
let parent = null;
const entry = (id, ms, body) => { const e = { id, parentId: parent, timestamp: iso(ms), ...body }; parent = id; return e; };
const msg = (id, ms, message) => entry(id, ms, { type: 'message', message: { timestamp: T0 + ms, ...message } });

/** A session: read + ls in parallel (ls fails), a write, an edit, a compaction, a model switch and a `!!` command. */
function session() {
  parent = null;
  return [
    entry('mc000001', 100, { type: 'model_change', provider: 'fixture', modelId: 'fixture-1' }),
    entry('tl000001', 100, { type: 'thinking_level_change', thinkingLevel: 'off' }),
    msg('sy000001', 200, { role: 'system', content: '', toolsAdded: [{ name: 'read' }, { name: 'ls' }, { name: 'write' }, { name: 'edit' }] }),
    msg('us000001', 200, { role: 'user', content: [{ type: 'text', text: 'Read hello.txt and make greet.py' }] }),
    { ...msg('as000001', 300, { role: 'assistant', provider: 'fixture', model: 'fixture-1', stopReason: 'toolUse', usage: usage(400, 40, 800),
      content: [{ type: 'text', text: 'Reading files.' }, { type: 'toolCall', id: 'call_r', name: 'read', arguments: { path: 'hello.txt' } },
        { type: 'toolCall', id: 'call_l', name: 'ls', arguments: { path: 'missing' } }] }), timestamp: iso(900) },
    msg('tr000001', 1000, { role: 'toolResult', toolCallId: 'call_r', toolName: 'read', isError: false, content: [{ type: 'text', text: 'hello-world\n' }] }),
    msg('tr000002', 1000, { role: 'toolResult', toolCallId: 'call_l', toolName: 'ls', isError: true, content: [{ type: 'text', text: 'ENOENT: missing' }] }),
    { ...msg('as000002', 1100, { role: 'assistant', provider: 'fixture', model: 'fixture-1', stopReason: 'toolUse', usage: usage(200, 30, 1200),
      content: [{ type: 'toolCall', id: 'call_w', name: 'write', arguments: { path: 'greet.py', content: 'def greet():\n    return "hi"\n' } }] }), timestamp: iso(1500) },
    msg('tr000003', 1600, { role: 'toolResult', toolCallId: 'call_w', toolName: 'write', isError: false, content: [{ type: 'text', text: 'Wrote greet.py' }] }),
    { ...msg('as000003', 1700, { role: 'assistant', provider: 'fixture', model: 'fixture-1', stopReason: 'toolUse', usage: usage(100, 20, 1450),
      content: [{ type: 'toolCall', id: 'call_e', name: 'edit', arguments: { path: 'greet.py', edits: [{ oldText: '"hi"', newText: '"hello"' }] } }] }), timestamp: iso(2000) },
    msg('tr000004', 2100, { role: 'toolResult', toolCallId: 'call_e', toolName: 'edit', isError: false, content: [{ type: 'text', text: 'Edited greet.py' }] }),
    { ...msg('as000004', 2200, { role: 'assistant', provider: 'fixture', model: 'fixture-1', stopReason: 'stop', usage: usage(80, 12, 1500),
      content: [{ type: 'text', text: 'Done: greet() returns "hello".' }] }), timestamp: iso(2500) },
    entry('cp000001', 3000, { type: 'compaction', summary: 'User asked for greet.py; it now returns hello.', firstKeptEntryId: 'cp000001', tokensBefore: 1592 }),
    entry('mc000002', 3100, { type: 'model_change', provider: 'fixture', modelId: 'fixture-2' }),
    msg('bx000001', 3200, { role: 'bashExecution', command: 'ls -la', output: 'greet.py\nhello.txt\n', exitCode: 0, cancelled: false, truncated: false, excludeFromContext: true }),
    msg('us000002', 3300, { role: 'user', content: [{ type: 'text', text: 'Thanks' }] }),
    { ...msg('as000005', 3400, { role: 'assistant', provider: 'fixture', model: 'fixture-2', stopReason: 'stop', usage: usage(300, 5, 0),
      content: [{ type: 'text', text: 'You are welcome.' }] }), timestamp: iso(3600) },
    entry('si000001', 3700, { type: 'session_info', name: 'Greeting module' }),
  ];
}

test('a Pi session projects to model, tool and harness steps with provider token counts', () => {
  const trace = projectSession(session(), { header, model: { provider: 'fixture', id: 'fixture-2', contextWindow: 64000 }, now: T0 + 5000 });
  assert.equal(trace.schema, 'kernelbridge.trace/v1');
  assert.equal(trace.session.id, 'sess-1'); assert.equal(trace.session.title, 'Greeting module'); assert.equal(trace.session.status, 'idle');
  assert.equal(trace.limit, 64000); assert.equal(trace.thresh, 64000 - 16384);
  assert.deepEqual(trace.labels, PI_LABELS); assert.deepEqual(trace.rows, ['M/main']);
  assert.deepEqual(trace.config.tools.sort(), ['edit', 'ls', 'read', 'write']);
  const models = trace.steps.filter(s => s.src === 'model');
  assert.equal(models.length, 5);
  assert.equal(models[0].tokIn, 1200, 'input + cacheRead + cacheWrite equals the provider prompt size');
  assert.equal(models[0].cacheReal, 800); assert.equal(models[0].nTools, 2);
  assert.equal(models[0].t0, 0.3, 'an assistant step starts at its request time'); assert.equal(models[0].t1, 0.9, 'and ends when Pi persisted it');
  assert.equal(models.at(-1).model, 'fixture-2'); assert.equal(trace.session.model, 'fixture/fixture-2');
  const read = trace.steps.find(s => s.tool === 'read'); const ls = trace.steps.find(s => s.tool === 'ls');
  assert.equal(read.status, 'ok'); assert.match(read.out, /hello-world/); assert.equal(read.title, '读取 hello.txt'); assert.equal(read.turn, models[0].id);
  assert.equal(ls.status, 'error'); assert.deepEqual([read.stack, ls.stack].sort(), [0, 1]);
  assert.equal(read.t0, 0.9); assert.equal(read.t1, 1);
});

test('context is calibrated to each next request, and a compaction ends at the following prompt', () => {
  const trace = projectSession(session(), { header, now: T0 + 5000 });
  const main = trace.steps.filter(s => s.agent === 'M/main');
  const models = main.filter(s => s.src === 'model');
  // Tool outputs between two requests add up to the growth of the prompt.
  for (let i = 1; i < 4; i++) {
    const between = main.filter(s => s.t0 >= models[i - 1].t1 && s.t0 < models[i].t0 && s.src === 'tool');
    if (between.length) assert.equal(models[i - 1].ctx + between.reduce((n, s) => n + s.outTok, 0), models[i].tokIn);
  }
  const compact = main.find(s => s.kind === 'compact');
  assert.equal(compact.before, 1592); assert.equal(compact.after, 300 - main.find(s => s.title.startsWith('用户：Thanks')).addTok);
  assert.match(compact.title, /^上下文压缩 1\.6k → 0\.3k$/);
});

test('markers, commands and file history come from session entries', () => {
  const trace = projectSession(session(), { header, now: T0 + 5000 });
  const checkpoints = trace.steps.filter(s => s.kind === 'checkpoint').map(s => s.title);
  assert.deepEqual(checkpoints, ['切换模型 fixture/fixture-2'], 'the header model and thinking entries are not markers');
  const bash = trace.steps.find(s => s.title.startsWith('命令'));
  assert.match(bash.title, /^命令（不进上下文） ls -la/); assert.equal(bash.addTok, 0);
  assert.deepEqual(trace.commits.map(c => [c.id, c.files[0].path]), [['E01', 'greet.py'], ['E02', 'greet.py']]);
  assert.deepEqual(trace.files['greet.py'].map(l => l.join('|')), ['def greet():|E01', '    return "hello"|E02', '|E01']);
});

test('live state: executing tools, a streaming reply and unfinished calls', () => {
  const entries = session().slice(0, 5); // the first reply's tool calls have no results yet
  const running = new Map([['call_r', { tool: 'read', args: { path: 'hello.txt' }, startedAt: T0 + 950 }]]);
  const live = projectSession(entries, { header, running, now: T0 + 4000 });
  assert.equal(live.steps.find(s => s.tool === 'read').status, 'running');
  assert.equal(live.steps.find(s => s.tool === 'read').t1, 4);
  assert.equal(live.steps.find(s => s.tool === 'ls').status, 'warn', 'a call with neither result nor execution is reported as unfinished');
  assert.equal(live.session.status, 'running');
  const streaming = projectSession(session().slice(0, 4), { header, streamingSince: T0 + 300, now: T0 + 1300 });
  const reply = streaming.steps.at(-1);
  assert.equal(reply.status, 'running'); assert.equal(reply.title, '生成中…'); assert.equal(reply.t0, 0.3); assert.equal(reply.t1, 1.3);
});

test('step ids are stable while the session grows', () => {
  const all = session(); const early = projectSession(all.slice(0, 9), { header, now: T0 + 1700 }); const later = projectSession(all, { header, now: T0 + 5000 });
  const ids = new Set(later.steps.map(s => s.id));
  for (const step of early.steps) assert.ok(ids.has(step.id), step.id);
});

test('the Pi page carries its own title, labels, and can drop web fonts', async () => {
  const trace = projectSession(session(), { header, now: T0 + 5000 });
  trace.session.title = '</script><script>alert(1)</script>';
  const page = await viewerPage(trace, { title: 'Pi · 实时轨迹' });
  assert.match(page, /<title>Pi · 实时轨迹<\/title>/);
  assert.ok(!page.includes('</script><script>alert(1)'));
  assert.ok(page.includes('"eyebrow":"Pi · 实时会话轨迹"'));
  assert.ok(page.includes('fonts.googleapis.com'));
  const offline = await viewerPage(trace, { title: 'Pi · 实时轨迹', offline: true });
  assert.ok(!offline.includes('fonts.googleapis.com') && !offline.includes('fonts.gstatic.com'));
  assert.ok((await viewerPage(trace)).includes('<title>KernelBridge · 实时轨迹</title>'), 'KernelBridge keeps its title by default');
});

function get(port, path, headers = {}) {
  return new Promise((accept, reject) => {
    const req = request({ host: '127.0.0.1', port, path, headers }, response => {
      let body = ''; response.setEncoding('utf8');
      response.on('data', chunk => { body += chunk; });
      response.on('end', () => accept({ status: response.statusCode, body }));
    });
    req.on('error', reject); req.end();
  });
}

test('the shared server follows a replaced source and pushes it over SSE', async () => {
  const entries = session();
  const server = await serveTrace({ snapshot: () => projectSession(entries.slice(0, 4), { header, now: T0 + 1000 }), unref: true });
  try {
    const base = new URL(server.url).pathname;
    assert.equal((await get(server.port, base, { host: 'evil.example:' + server.port })).status, 404);
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
    const wait = async predicate => { for (let i = 0; i < 100 && !predicate(); i++) await new Promise(r => setTimeout(r, 20)); assert.ok(predicate()); };
    await wait(() => events.length === 1);
    server.setSource(() => projectSession(entries, { header, now: T0 + 5000 }));
    await wait(() => events.length === 2);
    assert.ok(events[1].steps.length > events[0].steps.length);
    server.publish();
    await new Promise(r => setTimeout(r, 100));
    assert.equal(events.length, 2, 'an unchanged snapshot is not pushed again');
    const other = { ...header, id: 'sess-2' };
    server.setSource(() => projectSession([], { header: other, now: T0 }));
    await wait(() => events.length === 3);
    assert.equal(events[2].session.id, 'sess-2'); assert.equal(events[2].steps.length, 0);
    stream.destroy();
  } finally { await server.close(); }
});
