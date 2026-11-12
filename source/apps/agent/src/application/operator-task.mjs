/** Compile a clean, implementation-free operator task into an isolated application. */
import { createHash, randomUUID } from 'node:crypto';
import { mkdir, readFile, realpath, writeFile } from 'node:fs/promises';
import { dirname, isAbsolute, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { TextDecoder } from 'node:util';

const sha = bytes => createHash('sha256').update(bytes).digest('hex');
const save = (path, value) => writeFile(path, JSON.stringify(value, null, 2) + '\n', { mode: 0o600, flag: 'wx' });
const object = value => value && typeof value === 'object' && !Array.isArray(value);
function paths(values, name) {
  if (!Array.isArray(values) || !values.length || new Set(values).size !== values.length
      || values.some(path => typeof path !== 'string' || !path || isAbsolute(path) || path.split('/').some(part => !part || part === '.' || part === '..'))) {
    throw new Error(`${name} must contain distinct relative paths`);
  }
  return values;
}

function referenceText(bytes, path) {
  // Only explicitly admitted oracle/comparator source is considered below.
  // Never turn a credential file or a hard-coded secret into model input.
  const credentialPath = /(?:^|\/)(?:\.ssh|\.aws|\.kube|\.gnupg|\.netrc|\.npmrc|\.pypirc|\.git-credentials|\.env(?:\.[^/]*)?|credentials?(?:\.[^/]*)?|secrets?(?:\.[^/]*)?)(?:\/|$)/i;
  if (credentialPath.test(path) || bytes.length > 32 * 1024 * 1024) throw new Error('Reference is not a bounded reference source file');
  let text;
  try { text = new TextDecoder('utf-8', { fatal: true, ignoreBOM: true }).decode(bytes); }
  catch { return null; }
  // Binary references remain valid pinned pipeline inputs; they simply do not
  // become a lossy or potentially misleading model-visible text copy.
  if (text.includes('\0') || !Buffer.from(text, 'utf8').equals(bytes)) return null;
  const secret = /-----BEGIN [A-Z ]*PRIVATE KEY-----|\b(?:sk-[A-Za-z0-9_-]{16,}|AKIA[A-Z0-9]{16}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|xox[baprs]-[A-Za-z0-9-]{16,})\b/;
  const ambientSecrets = Object.entries(process.env).filter(([name, value]) =>
    /(?:TOKEN|SECRET|PASSWORD|API_?KEY|ACCESS_?KEY|PRIVATE_?KEY)/i.test(name) && value?.length >= 8).map(([, value]) => value);
  if (secret.test(text) || ambientSecrets.some(value => text.includes(value))) {
    throw new Error('Credential-like content cannot be mapped as a reference');
  }
  return text;
}

export async function prepareOperatorTask({ task, selected, output, agentRoot }) {
  if (task.schema_version !== 'kernelbridge.operator-task/v1' || selected.schema_version !== 'kernelbridge.operator-profile/v1') {
    throw new Error('Operator tasks require kernelbridge.operator-task/v1 and kernelbridge.operator-profile/v1');
  }
  if (Object.keys(task).some(key => !['schema_version', 'instruction', 'contract', 'files', 'writable', 'readonly'].includes(key))
      || typeof task.instruction !== 'string' || !task.instruction.trim() || !object(task.contract) || !object(task.files)) {
    throw new Error('Clean operator task requires instruction, contract and document files');
  }
  const writable = paths(task.writable, 'writable');
  const readonly = paths(task.readonly, 'readonly');
  if (writable.some(path => path === '.kernelbridge' || path === '.kernelbridge/agents' || path.startsWith('.kernelbridge/agents/'))) {
    throw new Error('Candidate paths conflict with host-owned agent definitions');
  }
  if ([...writable, ...readonly].some(path => path === 'refs' || path.startsWith('refs/')
      || path === 'resource-map.json' || path.startsWith('resource-map.json/'))) {
    throw new Error('Task paths conflict with generated reference resources');
  }
  if (Object.entries(task.files).some(([path, value]) => !readonly.includes(path) || typeof value !== 'string')
      || readonly.some(path => !Object.hasOwn(task.files, path))
      || writable.some(path => readonly.some(ro => path === ro || path.startsWith(ro + '/') || ro.startsWith(path + '/')))) {
    throw new Error('Clean tasks may seed readonly documents only; writable candidate sources must start absent');
  }
  const config = structuredClone(selected.config ?? {});
  if (await realpath(dirname(output)) !== dirname(output)) throw new Error('Trial output must not traverse symlinks');
  if (config.workspace_mode === 'project' || config.native_session_seed || config.rsi || config.evaluator || config.candidate_retention) {
    throw new Error('Clean operator trials require a fresh isolated workspace and explicit pipeline tools');
  }
  const tools = config.business_tools ?? [];
  if (!['pipeline_run', 'pipeline_pair', 'pipeline_promote'].every(name => tools.some(tool => tool.name === name))) {
    throw new Error('Operator profile must provide F, G and H pipeline tools');
  }
  const pins = new Map(), sources = new Map(), resources = [], copies = new Map();
  const seedFiles = structuredClone(task.files);
  async function sourceHashes(files, role) {
    if (!object(files) || !Object.keys(files).length) throw new Error('Acceptance requires pinned oracle and baseline files');
    const hashes = {};
    paths(Object.keys(files), 'source names');
    for (const [name, path] of Object.entries(files)) {
      if (typeof path !== 'string' || !isAbsolute(path) || await realpath(path) !== path) throw new Error('Acceptance source paths must be canonical absolute files');
      if (!sources.has(path)) {
        const bytes = await readFile(path);
        sources.set(path, { text: referenceText(bytes, path), sha256: sha(bytes) });
      }
      const source = sources.get(path);
      hashes[name] = source.sha256; pins.set(path, { path, sha256: source.sha256 });
      if (source.text !== null && !copies.has(source.sha256)) {
        const readPath = `refs/${source.sha256}/${name}`;
        copies.set(source.sha256, readPath); seedFiles[readPath] = source.text;
      }
      resources.push({ role, name, sha256: source.sha256, read_path: copies.get(source.sha256) ?? null, binding_path: path,
        access: source.text === null ? 'host_only_binary' : 'workspace_readonly' });
    }
    return hashes;
  }
  const contract = structuredClone(task.contract);
  const pipelineContract = structuredClone(contract);
  pipelineContract.oracle.files = await sourceHashes(contract.oracle?.files, 'oracle');
  const baseline = await sourceHashes(selected.acceptance?.baseline_files, 'baseline');
  if (typeof selected.acceptance?.scope !== 'string' || !selected.acceptance.scope) throw new Error('Acceptance requires the actual driver evidence scope');
  contract.acceptance = { kind: 'operator_pipeline', tool: 'pipeline_promote', evidence_tier: 'E3',
    source_paths: writable, pipeline_contract: pipelineContract, baseline_files: baseline, scope: selected.acceptance.scope };
  for (const tool of tools) {
    tool.files = [...tool.files, ...[...pins.values()].filter(pin => !tool.files.some(existing => existing.path === pin.path))];
  }
  seedFiles['resource-map.json'] = JSON.stringify({ schema_version: 'kernelbridge.operator-resources/v1', resources,
    usage: { native_read: 'Read non-null read_path relative to this workspace. These hash-identical reference copies are read-only. Null read_path means host_only_binary: use its binding_path through pipeline tools without a native text read.',
      pipeline_binding: 'Use binding_path only in pipeline_bind or pipeline_baseline; do not open host paths with native read/bash.',
      driver: 'Configured driver IDs are pipeline_evaluate/pipeline_run/pipeline_pair parameters, not shell commands. When pipeline_evaluate is available, use it to freeze, launch or recover the original job, collect and check in one bounded call; pairing and promotion remain separate.',
      bash: 'Each bash call uses a fresh temporary snapshot. Only successful authorized regular-file changes persist; failed or timed-out calls do not write changes back. The snapshot permits at most 256 KiB per file, 16 MiB total and 4096 files. TMPDIR files do not persist. Use relative paths; do not retain or cd to a previous temporary directory.',
      candidate: 'Create and reuse scratch files or helper scripts inside this workspace. Only declared candidate paths belong in the frozen source bundle; candidate runtime dependencies must be declared too. References, task documents and resource-map.json are read-only. No candidate implementation is provided.' } }, null, 2) + '\n';
  const generatedReadonly = ['refs', ...copies.values(), 'resource-map.json', '.kernelbridge/agents'];
  config.application = { ...selected.application, max_trials: selected.goal?.max_trials ?? selected.application?.max_trials,
    complete_on_acceptance: selected.goal?.complete_on_acceptance ?? true };
  const bindings = JSON.parse(await readFile(selected.expected_python_modules, 'utf8'));
  const roots = new Set(bindings.modules.flatMap(module => module.allowed_package_roots.map(root => dirname(fileURLToPath(root)))));
  if (roots.size !== 1) throw new Error('Expected one frozen installed Python module root');
  // task.writable declares the complete candidate bundle above. Workspace
  // helpers are independently writable, while admitted references stay frozen.
  Object.assign(config, { python_modules: bindings, workspace_mode: 'isolated', workspace: join(output, 'workspace'),
    state_dir: join(output, 'state'), writable_paths: null, readonly_paths: [...readonly, ...generatedReadonly],
    pi_worker: { ...config.pi_worker, node: selected.node ?? process.execPath, root: agentRoot } });
  const compiled = { id: 'operator-' + randomUUID(), scope: 'operator-development', goal: task.instruction,
    contract, seed_files: seedFiles };
  await mkdir(output, { mode: 0o700 });
  await save(join(output, 'task.json'), compiled);
  await save(join(output, 'config.json'), config);
  await save(join(output, 'prepared.json'), { schema_version: task.schema_version, model_started: false, device_started: false,
    candidate_seeded: false, writable, candidate_source_paths: writable,
    workspace_write_policy: { writable_paths: null, readonly_paths: [...readonly, ...generatedReadonly],
      shell_persistence: 'successful_authorized_regular_file_diff', temporary_files: 'per_call_disposable',
      max_file_bytes: 262144, max_tree_bytes: 16 * 1024 * 1024, max_files: 4096 },
    readonly: [...readonly, ...generatedReadonly], model_turn_accounting: 'all_model_calls_including_compact_child_review' });
  return { pythonPath: [...roots][0] };
}
