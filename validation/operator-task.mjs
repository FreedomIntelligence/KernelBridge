import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, readFile, realpath, rm, writeFile, symlink } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { prepareOperatorTask } from '../source/apps/agent/src/application/operator-task.mjs';
import { OPERATOR_WORKFLOW_PROMPT } from '../source/apps/agent/src/harness/pi/operator-workflow-prompt.mjs';

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
  const map = JSON.parse(task.seed_files['resource-map.json']);
  assert.equal(map.resources.length, 2);
  assert.equal(Object.keys(task.seed_files).length, 4);
  assert.equal(task.seed_files['contract.txt'], 'No candidate implementation.');
  for (const resource of map.resources) {
    const bytes = await readFile(resource.binding_path);
    assert.equal(Buffer.from(task.seed_files[resource.read_path]).equals(bytes), true);
    assert.equal(createHash('sha256').update(bytes).digest('hex'), resource.sha256);
    assert(config.readonly_paths.includes(resource.read_path));
  }
  assert(config.readonly_paths.includes('resource-map.json'));
  assert.equal(task.seed_files['candidate.py'], undefined);
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

test('reference copies deduplicate exact bytes and keep original pipeline identities unchanged', async t => {
  const args = await fixture(t);
  args.task.contract.oracle.files['eager.py'] = args.selected.acceptance.baseline_files['eager.py'];
  await writeFile(join(args.agentRoot, 'alias.py'), 'frozen comparator fixture');
  args.task.contract.oracle.files['alias.py'] = join(args.agentRoot, 'alias.py');
  await writeFile(join(args.agentRoot, 'candidate.py'), 'UNDECLARED_IMPLEMENTATION_MUST_NOT_BE_COPIED');
  const original = structuredClone(args.task);
  await prepareOperatorTask(args);
  const task = JSON.parse(await readFile(join(args.output, 'task.json'), 'utf8'));
  const config = JSON.parse(await readFile(join(args.output, 'config.json'), 'utf8'));
  const map = JSON.parse(task.seed_files['resource-map.json']);
  const repeated = map.resources.filter(item => item.name !== 'oracle.py');
  assert.equal(repeated.length, 3);
  assert.equal(new Set(repeated.map(item => item.read_path)).size, 1);
  assert.equal(Object.keys(task.seed_files).filter(name => name.startsWith('refs/')).length, 2);
  assert.equal(config.business_tools.every(tool => tool.files.length === 3), true);
  assert.deepEqual(task.contract.oracle, original.contract.oracle);
  assert.deepEqual(args.task, original, 'preparation must not mutate the authored contract');
  assert.equal(task.contract.acceptance.pipeline_contract.resource_map, undefined);
  assert.equal(Object.values(task.seed_files).some(text => text.includes('UNDECLARED_IMPLEMENTATION')), false);
});

test('generated resource paths cannot collide with authored readonly or writable paths', async t => {
  const args = await fixture(t);
  for (const reserved of ['refs', 'refs/input.py', 'resource-map.json', 'resource-map.json/child']) {
    args.task.readonly = ['contract.txt', reserved];
    args.task.files = { 'contract.txt': 'contract', [reserved]: 'collision' };
    await assert.rejects(prepareOperatorTask(args), /conflict with generated reference/);
  }
  args.task.readonly = ['contract.txt']; args.task.files = { 'contract.txt': 'contract' };
  args.task.writable = ['refs/candidate.py'];
  await assert.rejects(prepareOperatorTask(args), /conflict with generated reference/);
});

test('reference text keeps exact UTF-8 bytes including BOM', async t => {
  const args = await fixture(t);
  const path = args.task.contract.oracle.files['oracle.py'];
  const bytes = Buffer.from('\uFEFF# 中文 reference\n');
  await writeFile(path, bytes);
  await prepareOperatorTask(args);
  const task = JSON.parse(await readFile(join(args.output, 'task.json'), 'utf8'));
  const map = JSON.parse(task.seed_files['resource-map.json']);
  assert(Buffer.from(task.seed_files[map.resources[0].read_path]).equals(bytes));
});

test('non-UTF8 and NUL references retain their original pins and pipeline binding without a model text copy', async t => {
  for (const bytes of [Buffer.from([0xff, 0xfe]), Buffer.from('valid\0binary')]) {
    const args = await fixture(t);
    const path = args.task.contract.oracle.files['oracle.py'];
    await writeFile(path, bytes);
    args.selected.acceptance.baseline_files = { 'oracle.py': path };
    await prepareOperatorTask(args);
    const task = JSON.parse(await readFile(join(args.output, 'task.json'), 'utf8'));
    const config = JSON.parse(await readFile(join(args.output, 'config.json'), 'utf8'));
    const map = JSON.parse(task.seed_files['resource-map.json']);
    const sha256 = createHash('sha256').update(bytes).digest('hex');
    assert.equal(map.resources.length, 2);
    assert(map.resources.every(row => row.read_path === null && row.access === 'host_only_binary'
      && row.binding_path === path && row.sha256 === sha256));
    assert.equal(Object.keys(task.seed_files).some(name => name.startsWith('refs/')), false);
    assert.equal(task.contract.acceptance.pipeline_contract.oracle.files['oracle.py'], sha256);
    assert.equal(task.contract.acceptance.baseline_files['oracle.py'], sha256);
    assert(config.business_tools.every(tool => tool.files.length === 1
      && tool.files[0].path === path && tool.files[0].sha256 === sha256));
    assert((await readFile(path)).equals(bytes), 'preparation never changes a pinned binary');
  }
});

test('example passwords and credential placeholders are legitimate reference source text', async t => {
  const args = await fixture(t);
  const text = 'password = "example-password"\napi_key = "YOUR_API_KEY_HERE"\naccess_token = "placeholder-token"\n';
  await writeFile(args.task.contract.oracle.files['oracle.py'], text);
  await prepareOperatorTask(args);
  const task = JSON.parse(await readFile(join(args.output, 'task.json'), 'utf8'));
  const map = JSON.parse(task.seed_files['resource-map.json']);
  const reference = map.resources.find(row => row.role === 'oracle');
  assert.equal(reference.access, 'workspace_readonly');
  assert.equal(task.seed_files[reference.read_path], text);
});

test('symlinked sources and credential paths or contents never become model-visible reference files', async t => {
  const args = await fixture(t);
  const source = args.task.contract.oracle.files['oracle.py'];
  const link = join(args.agentRoot, 'link.py'); await symlink(source, link);
  args.task.contract.oracle.files = { 'oracle.py': link };
  await assert.rejects(prepareOperatorTask(args), /canonical absolute files/);
  const credentials = join(args.agentRoot, 'credentials.json'); await writeFile(credentials, '{}');
  args.task.contract.oracle.files = { 'oracle.py': credentials };
  await assert.rejects(prepareOperatorTask(args), /reference source file/);
  args.task.contract.oracle.files = { 'oracle.py': source };
  for (const text of ['-----BEGIN PRIVATE KEY-----\nfixture', 'api_key = "' + 'sk-' + 'x'.repeat(32) + '"']) {
    await writeFile(source, text);
    await assert.rejects(prepareOperatorTask(args), /Credential-like content/);
  }
  const name = 'KERNELBRIDGE_GROUNDING_FIXTURE_TOKEN', previous = process.env[name];
  try {
    process.env[name] = 'fixture-ambient-sensitive-value';
    await writeFile(source, `# ${process.env[name]}\n`);
    await assert.rejects(prepareOperatorTask(args), /Credential-like content/);
  } finally {
    if (previous === undefined) delete process.env[name]; else process.env[name] = previous;
  }
  await assert.rejects(readFile(join(args.output, 'task.json')), { code: 'ENOENT' });
});

test('resource guidance explains real tool scopes without changing limits or scheduling', () => {
  assert.match(OPERATOR_WORKFLOW_PROMPT, /resource-map\.json/);
  assert.match(OPERATOR_WORKFLOW_PROMPT, /read_path.*workspace-relative/);
  assert.match(OPERATOR_WORKFLOW_PROMPT, /binding_path.*not a native read\/bash path/);
  assert.match(OPERATOR_WORKFLOW_PROMPT, /fresh temporary snapshot/);
  assert.match(OPERATOR_WORKFLOW_PROMPT, /driver IDs are pipeline tool arguments/);
  assert.doesNotMatch(OPERATOR_WORKFLOW_PROMPT, /(?:remaining|total)\s+20|scatter_max|scatter_min|MatmulCustom/);
});
