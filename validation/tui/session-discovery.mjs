import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, writeFile, rm, utimes } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { sessions } from '../../source/apps/agent/src/application/host.mjs';

test('offline named sessions remain discoverable and sort by activity', async () => {
  const root = await mkdtemp(join(tmpdir(), 'kb-discovery-'));
  const previous = process.env.KERNELBRIDGE_SESSIONS;
  process.env.KERNELBRIDGE_SESSIONS = root;
  try {
    for (const [name, clock] of [['older', 100], ['resumed', 200]]) {
      const directory = join(root, 'application-' + name);
      const state = join(directory, 'state'); await mkdir(state, { recursive: true });
      const config = join(directory, 'config.json'), task = join(directory, 'task.json');
      await writeFile(config, JSON.stringify({ workspace: '/project', workspace_mode: 'project', state_dir: state }));
      await writeFile(task, JSON.stringify({ goal: 'default title' }));
      await writeFile(join(directory, 'descriptor.json'), JSON.stringify({ config, task, host_directory: directory, output: directory }));
      const journal = join(state, 'events.jsonl');
      await writeFile(journal, JSON.stringify({ kind: 'application/configuration', application_session_id: 'application-' + name,
        data: { operation: 'rename', name: 'title ' + name } }) + '\n' + '{"incomplete":');
      await utimes(journal, clock, clock);
    }
    const found = await sessions();
    assert.deepEqual(found.map(row => row.title), ['title resumed', 'title older']);
    assert.ok(found.every(row => row.connected === false && row.cwd === '/project'));
  } finally {
    if (previous === undefined) delete process.env.KERNELBRIDGE_SESSIONS;
    else process.env.KERNELBRIDGE_SESSIONS = previous;
    await rm(root, { recursive: true, force: true });
  }
});
