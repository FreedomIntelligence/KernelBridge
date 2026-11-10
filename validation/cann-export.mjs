import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, writeFile, readFile, symlink, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createHash } from 'node:crypto';
import { exportRun } from '../examples/cann/export.mjs';
import { materializedCandidate } from '../examples/cann/candidate.mjs';

test('export and continuation select the validated snapshot while preserving a later draft', async () => {
  const temp = await mkdtemp(join(tmpdir(), 'cann-final-identity-'));
  try {
    const source = join(temp, 'source');
    for (const dir of ['sdk', 'state/runtime-artifacts', 'workspace/task']) await mkdir(join(source, dir), { recursive: true });
    const frozen = 'validated = True\n', draft = 'unmeasured = True\n';
    const program = Buffer.from(JSON.stringify({ files: [{ path: 'solution.py', text: frozen }] }));
    const sha = createHash('sha256').update(program).digest('hex');
    const state = { pending_effect_ids: [], materialization: { result: { manifest_ref: {
      digest: 'sha256:' + sha, byte_length: program.length } } } };
    await writeFile(join(source, 'state/runtime-artifacts', sha), program);
    await writeFile(join(source, 'sdk/state.json'), JSON.stringify(state));
    await writeFile(join(source, 'sdk/result.json'), JSON.stringify({ status: 'progress', observation: { validation: { status: 'passed' } } }));
    await writeFile(join(source, 'workspace/task/solution.py'), draft);
    const output = join(temp, 'export');
    const manifest = await exportRun(source, output, { html: false });
    assert.equal(await readFile(join(output, 'candidate/solution.py'), 'utf8'), frozen);
    assert.equal(await readFile(join(output, 'workspace/solution.py'), 'utf8'), frozen);
    assert.equal(await readFile(join(output, 'workspace/task/solution.py'), 'utf8'), draft);
    const files = new Map(await Promise.all(manifest.files.map(async row => [row.path, await readFile(join(output, row.path))])));
    assert.equal(materializedCandidate(files, state).bytes.toString(), frozen);
    const accepted = JSON.parse(files.get('acceptance.json'));
    assert.equal(accepted.accepted, true);
    assert.equal(accepted.candidate_sha256, createHash('sha256').update(frozen).digest('hex'));
    await writeFile(join(source, 'state/runtime-artifacts', sha), 'altered');
    await assert.rejects(exportRun(source, join(temp, 'corrupt'), { html: false }), /missing or altered/);
  } finally { await rm(temp, { recursive: true, force: true }); }
});

test('run export retains original journal bytes, checks hashes and refuses overwrite', async () => {
  const temp = await mkdtemp(join(tmpdir(), 'cann-export-'));
  try {
    const source = join(temp, 'source'); await mkdir(join(source, 'state'), { recursive: true });
    const bytes = '{"original_action":"one"}\n';
    await writeFile(join(source, 'state/events.jsonl'), bytes);
    await writeFile(join(source, 'events.jsonl'), '{"event":"finished"}\n');
    const output = join(temp, 'export');
    const result = await exportRun(source, output, { html: false });
    assert.equal(await readFile(join(output, 'state/events.jsonl'), 'utf8'), bytes);
    assert.equal(result.files.find(x => x.path === 'state/events.jsonl').sha256, createHash('sha256').update(bytes).digest('hex'));
    await assert.rejects(exportRun(source, output, { html: false }));
    await symlink('/etc/hosts', join(source, 'external'));
    await assert.rejects(exportRun(source, join(temp, 'bad'), { html: false }), /symlink/);
  } finally { await rm(temp, { recursive: true, force: true }); }
});

test('credential-like content blocks export before creating output', async () => {
  const temp = await mkdtemp(join(tmpdir(), 'cann-secret-'));
  try {
    const source = join(temp, 'source'); await mkdir(source);
    await writeFile(join(source, 'config.json'), JSON.stringify({ accidental: 'sk-' + 'x'.repeat(32) }));
    await assert.rejects(exportRun(source, join(temp, 'export'), { html: false }), /Credential-like/);
  } finally { await rm(temp, { recursive: true, force: true }); }
});
