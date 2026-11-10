import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, writeFile, readFile, symlink, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createHash } from 'node:crypto';
import { exportRun } from '../examples/cann/export.mjs';

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
