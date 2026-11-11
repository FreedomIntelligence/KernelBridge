import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, writeFile, rm, utimes } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { sessions } from '../../source/apps/agent/src/application/host.mjs';
import { filterSessions, sessionPicker } from '../../source/apps/agent/src/application/session-picker.mjs';
import { projectInfo } from '../../source/apps/agent/src/application/project-info.mjs';

test('picker searches Chinese title, project and partial ID without changing the selected identity', () => {
  const rows = [
    { application_session_id: 'application-alpha', title: '保存的算子会话', cwd: '/project-A' },
    { application_session_id: 'application-beta', title: 'Other work', cwd: '/project-B' },
  ];
  assert.deepEqual(filterSessions(rows, '算子 PROJECT-A'), [rows[0]]);
  assert.deepEqual(filterSessions(rows, 'BETA'), [rows[1]]);
  assert.deepEqual(filterSessions(rows, 'missing'), []);
  let selected;
  const picker = sessionPicker(rows, id => { selected = id; }, () => {});
  picker.handleInput('算子'); picker.handleInput('\r');
  assert.equal(selected, 'application-alpha');
});

test('picker includes current session and switches project, branch and worktree scopes', () => {
  const metadata = (branch, common_directory = '/repo/.git') => ({ branch, common_directory });
  const row = (id, cwd, project) => ({ application_session_id: id, title: id, cwd, project, bytes: 10240, updated: Date.now() });
  const rows = [row('current', '/repo', metadata('main')), row('past-feature', '/repo', metadata('feature')),
    row('related-worktree', '/other-worktree', metadata('main')), row('unrelated', '/other-project', metadata('main', '/else/.git'))];
  let selected;
  const picker = sessionPicker(rows, id => { selected = id; }, () => {},
    { cwd: '/repo', project: metadata('main'), currentId: 'current' });
  const screen = () => picker.render(100).join('\n');
  assert.match(screen(), /current  \[当前\]/); assert.match(screen(), /1\/2/);
  assert.match(screen(), /日志 10.0 KB/); assert.doesNotMatch(screen(), /related-worktree|unrelated/);
  picker.handleInput('\x02'); assert.match(screen(), /1\/1/); assert.doesNotMatch(screen(), /past-feature/);
  picker.handleInput('\x17'); assert.match(screen(), /related-worktree/); assert.doesNotMatch(screen(), /unrelated/);
  picker.handleInput('\x01'); assert.match(screen(), /unrelated/);
  picker.handleInput('\x02'); assert.match(screen(), /1\/4/);
  picker.handleInput('\r'); assert.equal(selected, 'current');
  assert.ok(picker.render(48).every(line => !line.includes('undefined')));
});

test('project identity records the actual repository and branch without requiring a model', async () => {
  const value = await projectInfo(new URL('../..', import.meta.url).pathname);
  assert.ok(value?.common_directory.endsWith('/.git'));
  assert.ok(value.branch);
  assert.equal(await projectInfo('/nonexistent-kernelbridge-fixture'), null);
});

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
      const event = JSON.stringify({ kind: 'application/configuration', application_session_id: 'application-' + name,
        data: { operation: 'rename', name: 'title ' + name } });
      await writeFile(journal, event.replaceAll(':', ': ') + '\n' + '{"incomplete":');
      await utimes(journal, clock, clock);
    }
    const found = await sessions();
    assert.deepEqual(found.map(row => row.title), ['title resumed', 'title older']);
    assert.ok(found.every(row => row.connected === false && row.cwd === '/project' && row.has_history));
  } finally {
    if (previous === undefined) delete process.env.KERNELBRIDGE_SESSIONS;
    else process.env.KERNELBRIDGE_SESSIONS = previous;
    await rm(root, { recursive: true, force: true });
  }
});

test('authenticated registry remains discoverable inside an isolated Pi HOME', async () => {
  const root = await mkdtemp(join(tmpdir(), 'kb-home-discovery-'));
  const previous = { HOME: process.env.HOME, KERNELBRIDGE_SESSIONS: process.env.KERNELBRIDGE_SESSIONS };
  try {
    const registry = join(root, 'user-home/.kernelbridge/sessions');
    for (const name of ['old', 'empty']) {
      const directory = join(registry, 'application-' + name);
      const state = join(directory, 'state'); await mkdir(state, { recursive: true });
      const config = join(directory, 'config.json'), task = join(directory, 'task.json');
      await writeFile(config, JSON.stringify({ workspace: '/project', workspace_mode: 'project', state_dir: state }));
      await writeFile(task, JSON.stringify({ goal: 'passive startup' }));
      await writeFile(join(directory, 'descriptor.json'), JSON.stringify({ config, task, host_directory: directory, output: directory }));
      await writeFile(join(state, 'events.jsonl'), name === 'empty' ? '' : JSON.stringify({
        kind: 'application/input_accepted', application_session_id: 'application-old', data: { kind: 'prompt', text: '恢复以前的算子工作' },
      }) + '\n');
    }
    process.env.HOME = join(root, 'isolated-pi-home');
    delete process.env.KERNELBRIDGE_SESSIONS;
    assert.deepEqual(await sessions(), []);
    const found = await sessions({ registry });
    assert.equal(found.length, 2);
    assert.equal(found.find(item => item.application_session_id === 'application-old').title, '恢复以前的算子工作');
    assert.deepEqual(found.filter(item => item.has_history).map(item => item.application_session_id), ['application-old']);
  } finally {
    for (const [key, value] of Object.entries(previous)) {
      if (value === undefined) delete process.env[key]; else process.env[key] = value;
    }
    await rm(root, { recursive: true, force: true });
  }
});
