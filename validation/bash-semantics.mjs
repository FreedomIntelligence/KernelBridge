import test from 'node:test';
import assert from 'node:assert/strict';
import * as fs from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import * as sdk from '../source/apps/agent/node_modules/@earendil-works/pi-coding-agent/dist/index.js';
import { workspaceTools } from '../source/apps/agent/src/harness/pi/workspace-tools.mjs';
import { createNativeBridge } from '../source/apps/agent/src/harness/pi/native.mjs';

async function fixture(run) {
  const base = await fs.realpath(await fs.mkdtemp(join(tmpdir(), 'kb-bash-semantics-')));
  const root = join(base, 'workspace');
  await fs.mkdir(join(root, 'reference'), { recursive: true });
  await fs.writeFile(join(root, 'reference/spec.txt'), 'immutable reference');
  await fs.writeFile(join(root, 'solution.txt'), 'original');
  await fs.writeFile(join(base, 'private.txt'), 'SYNTHETIC_PRIVATE_MARKER');
  let case_sensitive = true;
  try { case_sensitive = (await fs.stat(join(root, 'REFERENCE'))).ino !== (await fs.stat(join(root, 'reference'))).ino; }
  catch (error) { if (error.code !== 'ENOENT') throw error; }
  const tool = workspaceTools(sdk, { root, tools: ['bash'], writable_paths: null,
    readonly_paths: ['reference', 'AGENTS.md'], case_sensitive })[0];
  const call = (command, options = {}) => tool.execute('draft', { command, timeout: 5, ...options.args }, options.signal);
  try { await run({ base, root, tool, call }); }
  finally { await fs.rm(base, { recursive: true, force: true }); }
}

test('ordinary failed subcommand allows later output and authorized writes', () => fixture(async ({ root, call }) => {
  const result = await call('false; printf continued; printf retained > helper.txt');
  assert.equal(result.structuredContent.exit_code, 0);
  assert.notEqual(result.isError, true);
  assert.equal(result.structuredContent.output, 'continued');
  assert.equal(await fs.readFile(join(root, 'helper.txt'), 'utf8'), 'retained');
}));

test('a nonzero final command rolls back earlier authorized writes', () => fixture(async ({ root, call }) => {
  const result = await call('printf partial > solution.txt; exit 23');
  assert.equal(result.structuredContent.exit_code, 23);
  assert.equal(result.isError, true);
  assert.equal(await fs.readFile(join(root, 'solution.txt'), 'utf8'), 'original');
}));

test('explicit errexit stops later output and rolls back earlier writes', () => fixture(async ({ root, call }) => {
  const result = await call('set -e; printf partial > solution.txt; false; printf forbidden; printf later > later.txt');
  assert.equal(result.structuredContent.exit_code, 1);
  assert.doesNotMatch(result.structuredContent.output, /forbidden/);
  assert.equal(await fs.readFile(join(root, 'solution.txt'), 'utf8'), 'original');
  await assert.rejects(fs.stat(join(root, 'later.txt')), { code: 'ENOENT' });
}));

test('default pipeline status comes from its last command', () => fixture(async ({ root, call }) => {
  const result = await call('printf retained > helper.txt; false | cat');
  assert.equal(result.structuredContent.exit_code, 0);
  assert.equal(await fs.readFile(join(root, 'helper.txt'), 'utf8'), 'retained');
}));

test('explicit pipefail changes final pipeline status and prevents writeback', () => fixture(async ({ root, call }) => {
  const result = await call('set -o pipefail; printf partial > solution.txt; false | cat');
  assert.equal(result.structuredContent.exit_code, 1);
  assert.equal(await fs.readFile(join(root, 'solution.txt'), 'utf8'), 'original');
}));

test('explicit pipefail alone does not imply errexit', () => fixture(async ({ call }) => {
  const result = await call('set -o pipefail; false | cat; printf continued');
  assert.equal(result.structuredContent.exit_code, 0);
  assert.equal(result.structuredContent.output, 'continued');
}));

test('explicit errexit and pipefail stop after a failing pipeline', () => fixture(async ({ root, call }) => {
  const result = await call('set -e; set -o pipefail; printf partial > solution.txt; false | cat; printf forbidden');
  assert.equal(result.structuredContent.exit_code, 1);
  assert.doesNotMatch(result.structuredContent.output, /forbidden/);
  assert.equal(await fs.readFile(join(root, 'solution.txt'), 'utf8'), 'original');
}));

test('readonly denial remains enforced when a later authorized command succeeds', () => fixture(async ({ root, call }) => {
  const result = await call('printf forbidden > reference/spec.txt; printf retained > helper.txt');
  assert.equal(result.structuredContent.exit_code, 0);
  assert.match(result.structuredContent.output, /not permitted|denied|read-only/i);
  assert.equal(await fs.readFile(join(root, 'reference/spec.txt'), 'utf8'), 'immutable reference');
  assert.equal(await fs.readFile(join(root, 'helper.txt'), 'utf8'), 'retained');
}));

test('host private files remain unreadable even when the command continues', () => fixture(async ({ base, call }) => {
  const result = await call(`cat '${join(base, 'private.txt')}'; printf continued`);
  assert.equal(result.structuredContent.exit_code, 0);
  assert.doesNotMatch(result.structuredContent.output, /SYNTHETIC_PRIVATE_MARKER/);
  assert.match(result.structuredContent.output, /not permitted|denied|No such file/i);
  assert.match(result.structuredContent.output, /continued/);
}));

test('synthetic host credentials and Python or shell startup environment are not inherited', () => fixture(async ({ base, call }) => {
  const startup = join(base, 'startup.sh');
  await fs.writeFile(startup, 'printf STARTUP_MUST_NOT_RUN\n');
  const supplied = { KB_DRAFT_SECRET: 'SYNTHETIC_SECRET_MARKER', OPENAI_API_KEY: 'SYNTHETIC_OPENAI_MARKER',
    VIRTUAL_ENV: '/synthetic/host-venv', PYTHONPATH: '/synthetic/host-modules', BASH_ENV: startup };
  const original = Object.fromEntries(Object.keys(supplied).map(key => [key, process.env[key]]));
  try {
    Object.assign(process.env, supplied);
    const result = await call('printf "%s|%s|%s|%s|%s" "${KB_DRAFT_SECRET-unset}" "${OPENAI_API_KEY-unset}" "${VIRTUAL_ENV-unset}" "${PYTHONPATH-unset}" "${BASH_ENV-unset}"');
    assert.equal(result.structuredContent.exit_code, 0);
    assert.equal(result.structuredContent.output, 'unset|unset|unset|unset|unset');
  } finally {
    for (const [key, value] of Object.entries(original)) {
      if (value === undefined) delete process.env[key]; else process.env[key] = value;
    }
  }
}));

test('timeout still kills the shell and prevents writeback', () => fixture(async ({ root, call }) => {
  await assert.rejects(call('printf partial > solution.txt; sleep 30', { args: { timeout: 0.1 } }), /timeout|timed out/i);
  assert.equal(await fs.readFile(join(root, 'solution.txt'), 'utf8'), 'original');
}));

test('cancellation still stops the shell and prevents writeback', () => fixture(async ({ root, call }) => {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 100);
  try { await assert.rejects(call('printf partial > solution.txt; sleep 30', { signal: controller.signal }), /abort|cancel/i); }
  finally { clearTimeout(timer); }
  assert.equal(await fs.readFile(join(root, 'solution.txt'), 'utf8'), 'original');
}));

function bridgeTool(tool, base, rpc, fail, receipts) {
  let registered;
  const bridge = createNativeBridge(sdk, rpc, [], base,
    (id, name, args) => ({ id, name, arguments: args }), fail, receipts, { tools: [tool], codemode: false });
  bridge.factories[0]({ registerTool(value) { registered = value; } });
  return registered;
}

test('native settlement retains real final status and original receipt is reused without shell execution', () => fixture(async ({ root, base, tool }) => {
  const observed = [], calls = [];
  let settled;
  const wrapped = bridgeTool(tool, base, { async call(method, params) {
    calls.push(method);
    if (method === 'native_begin') return settled
      ? { cached: true, action_id: 'original-action', outcome: settled }
      : { permit: 'original-permit' };
    assert.equal(method, 'native_settle');
    settled = { status: params.status, data: { result: params.result } };
    return { action_id: 'original-action', outcome: settled };
  } }, error => { throw error; }, { observeNative(event) { observed.push(event); } });
  const args = { command: 'false; printf once > helper.txt', timeout: 5 };
  const first = await wrapped.execute('original-action', args);
  assert.equal(settled.status, 'completed');
  assert.equal(settled.data.result.structuredContent.exit_code, 0);
  assert.match(first.content.at(-1).text, /Execution receipt: original-permit/);
  await fs.writeFile(join(root, 'helper.txt'), 'changed after original receipt');
  const cached = await wrapped.execute('original-action', args);
  assert.deepEqual(cached, settled.data.result);
  assert.deepEqual(cached.content, first.content);
  assert.deepEqual(cached.structuredContent, first.structuredContent);
  assert.equal(await fs.readFile(join(root, 'helper.txt'), 'utf8'), 'changed after original receipt');
  assert.deepEqual(calls, ['native_begin', 'native_settle', 'native_begin']);
  assert.equal(observed.length, 2);
  assert.equal(observed[0].response.action_id, observed[1].response.action_id);
}));

test('lost settlement propagates once without replaying or inventing a confirmed receipt', () => fixture(async ({ root, base, tool }) => {
  const calls = [], failures = [], observed = [];
  const wrapped = bridgeTool(tool, base, { async call(method) {
    calls.push(method);
    if (method === 'native_begin') return { permit: 'uncertain-permit' };
    assert.equal(method, 'native_settle');
    throw new Error('original settlement unavailable');
  } }, error => failures.push(error), { observeNative(event) { observed.push(event); } });
  await assert.rejects(wrapped.execute('uncertain-action', { command: 'false; printf once > helper.txt', timeout: 5 }), /original settlement unavailable/);
  assert.equal(await fs.readFile(join(root, 'helper.txt'), 'utf8'), 'once');
  assert.deepEqual(calls, ['native_begin', 'native_settle']);
  assert.equal(failures.length, 1);
  assert.equal(observed.length, 0);
}));

test('model-visible description states final-status and isolated-environment semantics', () => fixture(async ({ tool }) => {
  assert.match(tool.description, /environment is cleared/i);
  assert.match(tool.description, /credentials/);
  assert.match(tool.description, /final status/);
  assert.match(tool.description, /explicitly enable pipefail/);
  assert.match(tool.description, /set -e and set -o pipefail/);
}));
