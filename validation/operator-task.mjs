import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, readFile, realpath, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { prepareOperatorTask } from '../source/apps/agent/src/application/operator-task.mjs';

async function fixture(t) {
  const root = await realpath(await mkdtemp(join(tmpdir(), 'kb-clean-task-')));
  t.after(() => rm(root, { recursive: true, force: true }));
  await writeFile(join(root, 'oracle.py'), 'independent checker fixture');
  await writeFile(join(root, 'eager.py'), 'frozen comparator fixture');
  await writeFile(join(root, 'modules.json'), JSON.stringify({ modules: [{ allowed_package_roots: [pathToFileURL(join(root, 'site-packages', 'worker')).href] }] }));
  const task = { schema_version: 'kernelbridge.operator-task/v1', instruction: 'Implement the declared operator.',
    contract: { task: { name: 'other-operator' }, oracle: { files: { 'oracle.py': join(root, 'oracle.py') } },
      suite: [{ id: 'complete-case' }], measurement: { scope: 'fixture' } },
    files: { 'contract.txt': 'No candidate implementation.' }, writable: ['candidate.py'], readonly: ['contract.txt'] };
  const selected = { schema_version: 'kernelbridge.operator-profile/v1', expected_python_modules: join(root, 'modules.json'),
    config: { business_tools: ['pipeline_run', 'pipeline_pair', 'pipeline_promote'].map(name => ({ name, files: [] })) },
    application: { max_model_turns: 20, token_budget: null, max_forks: 0 },
    goal: { max_trials: 1, complete_on_acceptance: false },
    acceptance: { scope: 'fixture', baseline_files: { 'eager.py': join(root, 'eager.py') } } };
  return { task, selected, output: join(root, 'run'), agentRoot: root };
}

test('clean task freezes documents and comparator while preserving strict shared limits', async t => {
  const args = await fixture(t);
  await prepareOperatorTask(args);
  const task = JSON.parse(await readFile(join(args.output, 'task.json'), 'utf8'));
  const config = JSON.parse(await readFile(join(args.output, 'config.json'), 'utf8'));
  assert.deepEqual(Object.keys(task.seed_files), ['contract.txt']);
  assert.equal(config.workspace_mode, 'isolated');
  assert.equal(config.application.max_model_turns, 20);
  assert.equal(config.application.max_trials, 1);
  assert.equal(config.application.complete_on_acceptance, false);
  assert.equal(config.credits, undefined);
  assert.deepEqual(config.writable_paths, ['candidate.py']);
  assert.match(task.contract.acceptance.baseline_files['eager.py'], /^[a-f0-9]{64}$/);
  assert.equal(config.business_tools.every(tool => tool.files.length === 2), true);
  await assert.rejects(readFile(join(config.workspace, 'candidate.py')), { code: 'ENOENT' });
  await assert.rejects(prepareOperatorTask(args), { code: 'EEXIST' });
});

test('clean task rejects a seeded implementation and readonly/writable overlap', async t => {
  const args = await fixture(t);
  args.task.files['candidate.py'] = 'prewritten answer';
  await assert.rejects(prepareOperatorTask(args), /seed readonly documents only/);
  args.task.readonly.push('candidate.py');
  await assert.rejects(prepareOperatorTask(args), /seed readonly documents only/);
});

test('isolated task cannot import a previous native session or project workspace', async t => {
  const args = await fixture(t);
  args.selected.config.native_session_seed = { path: '/old/session' };
  await assert.rejects(prepareOperatorTask(args), /fresh isolated workspace/);
  delete args.selected.config.native_session_seed;
  args.selected.config.workspace_mode = 'project';
  await assert.rejects(prepareOperatorTask(args), /fresh isolated workspace/);
});
