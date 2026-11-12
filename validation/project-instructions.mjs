import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { mkdtemp, mkdir, realpath, rm, symlink, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { projectInstructions } from '../source/apps/agent/src/harness/pi/context-files.mjs';

const sdkUrl = new URL('../source/apps/agent/node_modules/@earendil-works/pi-coding-agent/dist/index.js', import.meta.url);
const { DefaultResourceLoader, SettingsManager } = await import(sdkUrl.href);
const { buildSystemPrompt } = await import(new URL('./core/system-prompt.js', sdkUrl));
const file = (path, content) => ({ path, content, bytes: Buffer.byteLength(content, 'utf8'),
  sha256: createHash('sha256').update(content, 'utf8').digest('hex') });
const snapshot = (...files) => ({ schema_version: 'kernelbridge.project-instructions/v1', files });

async function workspace(t) {
  const root = await realpath(await mkdtemp(join(process.env.KERNELBRIDGE_TEST_TMP ?? tmpdir(), 'kb-instructions-')));
  t.after(() => rm(root, { recursive: true, force: true }));
  const cwd = join(root, 'project'), agentDir = join(root, 'agent');
  await Promise.all([mkdir(cwd), mkdir(agentDir)]);
  return { root, cwd, agentDir };
}

async function loaderFor(cwd, agentDir, frozen) {
  const loader = new DefaultResourceLoader({ cwd, agentDir,
    settingsManager: SettingsManager.inMemory({}, { projectTrusted: false }),
    noExtensions: true, noSkills: true, noPromptTemplates: true, noThemes: true, noContextFiles: true,
    systemPrompt: 'Fixture coding instructions.', appendSystemPrompt: [],
    agentsFilesOverride: () => structuredClone(frozen) });
  await loader.reload();
  assert.deepEqual(loader.getExtensions().errors, []);
  return loader;
}

function systemPrompt(loader, cwd) {
  return buildSystemPrompt({ cwd, customPrompt: loader.getSystemPrompt(), selectedTools: [],
    contextFiles: loader.getAgentsFiles().agentsFiles, skills: [] });
}

test('admitted root instruction bytes are delivered without an existing workspace', () => {
  const cwd = '/nonexistent-host-admitted-workspace';
  const admitted = snapshot(file('AGENTS.md', 'ROOT_GUIDANCE λ\n'), file('AGENTS.task.md', '任务 appendix\n'));
  assert.deepEqual(projectInstructions(cwd, admitted), { agentsFiles: [
    { path: join(cwd, 'AGENTS.md'), content: 'ROOT_GUIDANCE λ\n' },
    { path: join(cwd, 'AGENTS.task.md'), content: '任务 appendix\n' },
  ] });
  assert.deepEqual(projectInstructions(cwd, snapshot(admitted.files[1])).agentsFiles.map(value => value.content), ['任务 appendix\n']);
});

test('invalid host snapshot schema cannot trigger filesystem fallback', async t => {
  const { cwd } = await workspace(t);
  await writeFile(join(cwd, 'AGENTS.md'), 'UNADMITTED_FALLBACK');
  for (const admitted of [null, {}, [], { schema_version: 'other', files: [] },
    { schema_version: 'kernelbridge.project-instructions/v1', files: {} }]) {
    assert.throws(() => projectInstructions(cwd, admitted), /Invalid host instruction snapshot/);
  }
});

test('host instructions reject unknown nested absolute duplicate and reversed paths', () => {
  for (const name of ['README.md', 'agents.md', 'nested/AGENTS.md', '../AGENTS.md', '/AGENTS.md']) {
    assert.throws(() => projectInstructions('/workspace', snapshot(file(name, 'text'))), /Invalid host instruction snapshot file/);
  }
  const root = file('AGENTS.md', 'root'), task = file('AGENTS.task.md', 'task');
  for (const files of [[root, root], [task, root], [task, task]]) {
    assert.throws(() => projectInstructions('/workspace', snapshot(...files)), /Invalid host instruction snapshot file/);
  }
});

test('changed content and forged hash are rejected even when byte length matches', () => {
  const original = file('AGENTS.md', 'trusted');
  for (const changed of [{ ...original, content: 'changed' }, { ...original, sha256: '0'.repeat(64) },
    { ...original, sha256: original.sha256.toUpperCase() }]) {
    assert.throws(() => projectInstructions('/workspace', snapshot(changed)), /Invalid host instruction snapshot file/);
  }
});

test('host byte accounting uses UTF-8 bytes and rejects noninteger or wrong byte counts', () => {
  const original = file('AGENTS.md', '指南🙂');
  assert.equal(original.bytes, 10);
  assert.equal(projectInstructions('/workspace', snapshot(original)).agentsFiles[0].content, original.content);
  for (const bytes of [original.content.length, -1, 10.5, '10', 11]) {
    assert.throws(() => projectInstructions('/workspace', snapshot({ ...original, bytes })), /Invalid host instruction snapshot file/);
  }
  assert.throws(() => projectInstructions('/workspace', snapshot({ ...original, content: null })), /Invalid host instruction snapshot file/);
});

test('host instruction bound accepts exactly 128 KiB and rejects one additional byte', () => {
  const exact = file('AGENTS.md', 'λ'.repeat(65536));
  assert.equal(exact.bytes, 131072);
  assert.equal(projectInstructions('/workspace', snapshot(exact)).agentsFiles[0].content, exact.content);
  assert.throws(() => projectInstructions('/workspace', snapshot(file('AGENTS.md', exact.content + 'x'))),
    /Invalid host instruction snapshot file/);
});

test('host input and returned instruction objects have no shared mutable references', () => {
  const admitted = snapshot(file('AGENTS.md', 'ORIGINAL'));
  const first = projectInstructions('/workspace', admitted);
  first.agentsFiles[0].content = 'CALLER_MUTATION';
  assert.equal(admitted.files[0].content, 'ORIGINAL');
  const second = projectInstructions('/workspace', admitted);
  admitted.files[0].content = 'LATER_HOST_OBJECT_MUTATION';
  admitted.files.push(file('AGENTS.task.md', 'LATER_APPENDIX'));
  assert.deepEqual(second, { agentsFiles: [{ path: '/workspace/AGENTS.md', content: 'ORIGINAL' }] });
});

test('explicit empty host instructions suppress local and symlink fallback', async t => {
  const { root, cwd } = await workspace(t);
  await writeFile(join(cwd, 'AGENTS.md'), 'UNADMITTED_LOCAL');
  await writeFile(join(root, 'external.md'), 'UNADMITTED_EXTERNAL');
  await symlink(join(root, 'external.md'), join(cwd, 'AGENTS.task.md'));
  assert.deepEqual(projectInstructions(cwd, snapshot()), { agentsFiles: [] });
});

test('host snapshot takes precedence over different or symlinked current filesystem contents', async t => {
  const { root, cwd } = await workspace(t);
  await writeFile(join(root, 'external.md'), 'UNADMITTED_EXTERNAL');
  await symlink(join(root, 'external.md'), join(cwd, 'AGENTS.md'));
  await writeFile(join(cwd, 'AGENTS.task.md'), 'UNADMITTED_LOCAL');
  const admitted = snapshot(file('AGENTS.md', 'HOST_ROOT'), file('AGENTS.task.md', 'HOST_TASK'));
  assert.deepEqual(projectInstructions(cwd, admitted).agentsFiles.map(value => value.content), ['HOST_ROOT', 'HOST_TASK']);
});

test('legacy embedders load only root instructions in fixed order', async t => {
  const { root, cwd, agentDir } = await workspace(t);
  await mkdir(join(cwd, 'nested'));
  await Promise.all([writeFile(join(root, 'AGENTS.md'), 'ANCESTOR'),
    writeFile(join(agentDir, 'AGENTS.md'), 'GLOBAL'), writeFile(join(cwd, 'nested/AGENTS.md'), 'NESTED'),
    writeFile(join(cwd, 'AGENTS.task.md'), 'TASK'), writeFile(join(cwd, 'AGENTS.md'), 'ROOT')]);
  assert.deepEqual(projectInstructions(cwd).agentsFiles, [
    { path: join(cwd, 'AGENTS.md'), content: 'ROOT' }, { path: join(cwd, 'AGENTS.task.md'), content: 'TASK' },
  ]);
});

test('legacy embedders without root instruction files receive no context files', async t => {
  const { root, cwd } = await workspace(t);
  await writeFile(join(root, 'AGENTS.md'), 'ANCESTOR');
  assert.deepEqual(projectInstructions(cwd), { agentsFiles: [] });
});

test('legacy filesystem discovery rejects symlinked instruction files', async t => {
  const { root, cwd } = await workspace(t);
  await writeFile(join(root, 'external.md'), 'EXTERNAL');
  await symlink(join(root, 'external.md'), join(cwd, 'AGENTS.md'));
  assert.throws(() => projectInstructions(cwd), /Invalid admitted instruction file/);
});

test('legacy filesystem discovery rejects directories and oversized UTF-8 files', async t => {
  const { cwd } = await workspace(t);
  await mkdir(join(cwd, 'AGENTS.md'));
  assert.throws(() => projectInstructions(cwd), /Invalid admitted instruction file/);
  await rm(join(cwd, 'AGENTS.md'), { recursive: true });
  await writeFile(join(cwd, 'AGENTS.md'), 'λ'.repeat(65536) + 'x');
  assert.throws(() => projectInstructions(cwd), /Invalid admitted instruction file/);
});

test('real Pi resource reload and system builder preserve the frozen admitted bytes', async t => {
  const { cwd, agentDir } = await workspace(t);
  await writeFile(join(cwd, 'AGENTS.md'), 'FILESYSTEM_BEFORE');
  const admitted = snapshot(file('AGENTS.md', 'FROZEN_ROOT λ'), file('AGENTS.task.md', 'FROZEN_APPENDIX'));
  const frozen = projectInstructions(cwd, admitted);
  const loader = await loaderFor(cwd, agentDir, frozen);
  const original = systemPrompt(loader, cwd);
  assert.match(original, /FROZEN_ROOT λ/);
  assert.match(original, /FROZEN_APPENDIX/);
  assert.ok(original.indexOf('FROZEN_ROOT λ') < original.indexOf('FROZEN_APPENDIX'));
  assert.ok(!original.includes('FILESYSTEM_BEFORE'));
  await writeFile(join(cwd, 'AGENTS.md'), 'FILESYSTEM_AFTER');
  admitted.files[0].content = 'MUTATED_HOST_OBJECT';
  loader.getAgentsFiles().agentsFiles[0].content = 'MUTATED_LOADER_VIEW';
  await loader.reload();
  assert.equal(systemPrompt(loader, cwd), original);
  assert.equal(frozen.agentsFiles[0].content, 'FROZEN_ROOT λ');
});

test('real Pi explicit empty override excludes project ancestor and global instruction files', async t => {
  const { root, cwd, agentDir } = await workspace(t);
  await Promise.all([writeFile(join(root, 'AGENTS.md'), 'UNADMITTED_ANCESTOR'),
    writeFile(join(cwd, 'AGENTS.md'), 'UNADMITTED_PROJECT'), writeFile(join(agentDir, 'AGENTS.md'), 'UNADMITTED_GLOBAL')]);
  const loader = await loaderFor(cwd, agentDir, projectInstructions(cwd, snapshot()));
  assert.deepEqual(loader.getAgentsFiles(), { agentsFiles: [] });
  assert.ok(!systemPrompt(loader, cwd).includes('UNADMITTED_'));
});
