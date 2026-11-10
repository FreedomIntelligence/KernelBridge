import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, writeFile, readFile, symlink, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { indexRun, exportRun } from '../examples/cann/export.mjs';
import { restoreSession } from '../examples/cann/restore-session.mjs';
import { executePrepared } from '../examples/cann/run.mjs';

async function fixture(root, pending = false) {
  const folder = join(root, 'source');
  await mkdir(join(folder, 'state/pi-sessions/session/sessions'), { recursive: true });
  const entries = [
    { type: 'session', version: 3, id: 'portable-test', cwd: '/unavailable/original/workspace', timestamp: new Date().toISOString() },
    { type: 'message', id: 'one', parentId: null, timestamp: new Date().toISOString(), message: {
      role: 'user', content: 'Original goal', timestamp: Date.now() } },
  ];
  if (pending) entries.push({ type: 'message', id: 'two', parentId: 'one', timestamp: new Date().toISOString(), message: {
    role: 'assistant', content: [{ type: 'toolCall', id: 'missing', name: 'evaluate', arguments: {} }], timestamp: Date.now() } });
  await writeFile(join(folder, 'state/pi-sessions/session/sessions/original.jsonl'), entries.map(x => JSON.stringify(x)).join('\n') + '\n');
  await indexRun(folder);
  return folder;
}

test('restore preserves original session bytes and uses a new folder with spaces', async () => {
  const temp = await mkdtemp(join(tmpdir(), 'portable restore '));
  try {
    const source = await fixture(temp);
    const before = await readFile(join(source, 'state/pi-sessions/session/sessions/original.jsonl'));
    const output = join(temp, 'new environment');
    const report = await restoreSession(source, output);
    assert.equal(report.sessions[0].session_id, 'portable-test');
    assert.equal(report.sessions[0].messages, 1);
    assert.deepEqual(await readFile(join(output, 'session.jsonl')), before);
    assert.deepEqual(await readFile(join(source, 'state/pi-sessions/session/sessions/original.jsonl')), before);
    assert.ok(report.continue_argv.includes('--restored'));
    assert.equal(report.pi_open_argv, undefined);
    await assert.rejects(restoreSession(source, output), /EEXIST/);
  } finally { await rm(temp, { recursive: true, force: true }); }
});

test('restore refuses altered bytes, path traversal, symlinks and unresolved tool exchanges', async () => {
  for (const failure of ['bytes', 'path', 'symlink', 'pending']) {
    const temp = await mkdtemp(join(tmpdir(), 'restore-invalid-'));
    try {
      const source = await fixture(temp, failure === 'pending');
      const manifestPath = join(source, 'manifest.json');
      const manifest = JSON.parse(await readFile(manifestPath));
      const session = join(source, manifest.indices.pi_sessions[0]);
      if (failure === 'bytes') await writeFile(session, 'changed');
      if (failure === 'path') { manifest.files[0].path = '../external'; await writeFile(manifestPath, JSON.stringify(manifest)); }
      if (failure === 'symlink') { const bytes = await readFile(session); await rm(session); await writeFile(join(temp, 'external'), bytes); await symlink(join(temp, 'external'), session); }
      await assert.rejects(restoreSession(source, join(temp, 'restored')));
    } finally { await rm(temp, { recursive: true, force: true }); }
  }
});

test('export cannot turn failed Goal or pending effects into acceptance', async () => {
  for (const [status, pending] of [['failed', []], ['progress', ['unknown-effect']], ['progress', []]]) {
    const temp = await mkdtemp(join(tmpdir(), 'acceptance-export-'));
    try {
      const source = join(temp, 'source'); await mkdir(join(source, 'sdk'), { recursive: true });
      await writeFile(join(source, 'sdk/result.json'), JSON.stringify({ status, observation: { validation: { status: 'passed' } } }));
      await writeFile(join(source, 'sdk/state.json'), JSON.stringify({ pending_effect_ids: pending }));
      const output = join(temp, 'export'); await exportRun(source, output, { html: false });
      const acceptance = JSON.parse(await readFile(join(output, 'acceptance.json')));
      assert.equal(acceptance.accepted, status === 'progress' && pending.length === 0);
    } finally { await rm(temp, { recursive: true, force: true }); }
  }
});

test('missing credential retains failure and manifest before any model starts', async () => {
  const temp = await mkdtemp(join(tmpdir(), 'missing-credential-'));
  const old = process.env.PYTHONPATH;
  try {
    await writeFile(join(temp, 'config.json'), JSON.stringify({ providers: { primary: { api_key_env: 'KB_TEST_ABSENT_CREDENTIAL' } } }));
    delete process.env.KB_TEST_ABSENT_CREDENTIAL;
    await assert.rejects(executePrepared({ output: temp, pythonPath: '/fixture/modules', mode: 'optimize' }), /Set KB_TEST/);
    assert.ok(JSON.parse(await readFile(join(temp, 'failure.json'))).error.includes('KB_TEST'));
    assert.ok(JSON.parse(await readFile(join(temp, 'manifest.json'))).files.some(x => x.path === 'failure.json'));
    assert.equal(process.env.PYTHONPATH, old);
  } finally { await rm(temp, { recursive: true, force: true }); }
});
