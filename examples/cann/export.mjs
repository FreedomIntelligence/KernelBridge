/** Read-only copying/indexing of a completed run; does not invoke the agent or evaluator. */
import { cp, lstat, mkdir, readdir, readFile, writeFile, access } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { dirname, join, relative, resolve, isAbsolute } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { materializedCandidate, candidateHash } from './candidate.mjs';
const here = dirname(fileURLToPath(import.meta.url));
const hash = bytes => createHash('sha256').update(bytes).digest('hex');
const exists = path => access(path).then(() => true, () => false);
const sensitive = /(?:gh[pousr]_[A-Za-z0-9]{30,}|sk-[A-Za-z0-9]{24,})/;

async function files(root, dir = root) {
  const result = [];
  for (const entry of await readdir(dir, { withFileTypes: true })) {
    const path = join(dir, entry.name);
    if (entry.isSymbolicLink()) throw new Error(`Export refuses symlink: ${path}`);
    if (entry.isDirectory()) result.push(...await files(root, path));
    else if (entry.isFile()) result.push(relative(root, path));
    else throw new Error(`Export refuses special file: ${path}`);
  }
  return result.sort();
}

export async function indexRun(root, { html = false } = {}) {
  root = resolve(root);
  if (html) {
    const exporter = await import(pathToFileURL(resolve(here, '../../source/apps/agent/node_modules/@earendil-works/pi-coding-agent/dist/core/export-html/index.js')).href);
    for (const name of await files(root)) {
      if (name.includes('/pi-sessions/') && name.includes('/sessions/') && name.endsWith('.jsonl')) {
        const target = join(root, name + '.html');
        if (!await exists(target)) await exporter.exportFromFile(join(root, name), { outputPath: target });
      }
    }
  }
  const listing = await files(root);
  const rows = [];
  for (const name of listing.filter(x => x !== 'manifest.json')) {
    const data = await readFile(join(root, name));
    if (sensitive.test(data.toString('utf8'))) throw new Error(`Credential-like content blocks export: ${name}`);
    rows.push({ path: name, bytes: data.length, sha256: hash(data) });
  }
  const manifest = { schema_version: 'kernelbridge.run-archive/v1', files: rows,
    indices: {
      sdk_events: listing.filter(x => x === 'events.jsonl' || x === 'sdk/events.jsonl'),
      execution_journals: listing.filter(x => x === 'state/events.jsonl'),
      pi_sessions: listing.filter(x => x.includes('/pi-sessions/') && x.includes('/sessions/') && x.endsWith('.jsonl')),
      pi_html: listing.filter(x => x.includes('/pi-sessions/') && x.endsWith('.html')),
      evaluations: listing.filter(x => /(^evaluations\/|\/evaluations\/|\/rsi-evaluations\/|\/rsi-private\/)/.test(x)),
      loopx: listing.filter(x => x.startsWith('loopx/') || x.startsWith('sdk/loopx/')),
      acceptance: listing.filter(x => x === 'acceptance.json' || x === 'sdk/goal-turn.json'),
      candidate: listing.filter(x => x.startsWith('candidate/')),
    }, scope: 'Copied evidence, no model/device execution. Original absolute locators are preserved; use relative file indices after relocation.' };
  await writeFile(join(root, 'manifest.json'), JSON.stringify(manifest, null, 2) + '\n');
  return manifest;
}

export async function exportRun(source, output, { html = true } = {}) {
  source = resolve(source); output = resolve(output);
  if (output === source || !relative(source, output).startsWith('..') || !relative(output, source).startsWith('..')) throw new Error('Source and export must be disjoint');
  if ((await lstat(source)).isSymbolicLink()) throw new Error('Source cannot be a symlink');
  const names = await files(source);
  const originals = new Map();
  for (const name of names) {
    const bytes = await readFile(join(source, name));
    if (sensitive.test(bytes.toString('utf8'))) throw new Error(`Credential-like content blocks export: ${name}`);
    originals.set(name, bytes);
  }
  const state = originals.has('sdk/state.json') ? JSON.parse(originals.get('sdk/state.json')) : null;
  let candidate = materializedCandidate(originals, state);
  const originalAcceptance = originals.has('acceptance.json') ? JSON.parse(originals.get('acceptance.json')) : null;
  if (!state && originalAcceptance?.accepted === true) {
    const entry = [...originals].find(([path, bytes]) => path.startsWith('workspace/') && path.endsWith('/solution.py')
      && candidateHash(bytes) === originalAcceptance.candidate_sha256);
    if (!entry) throw new Error('Accepted final candidate bytes are missing or altered');
    candidate = { bytes: entry[1], identity: { sha256: candidateHash(entry[1]), bytes: entry[1].length,
      basis: 'acceptance.json#candidate_sha256', source_path: entry[0] } };
  }
  await mkdir(output, { mode: 0o700 });
  for (const name of names) {
    const target = join(output, name);
    await mkdir(dirname(target), { recursive: true });
    await cp(join(source, name), target, { force: false, errorOnExist: true });
  }
  const aliases = [['config.json', 'config.redacted.json'], ['sdk/events.jsonl', 'events.jsonl'], ['sdk/result.json', 'result.json']];
  for (const [src, dst] of aliases) if (!await exists(join(output, dst)) && await exists(join(output, src))) await cp(join(output, src), join(output, dst));
  if (await exists(join(output, 'sdk/loopx')) && !await exists(join(output, 'loopx'))) await cp(join(output, 'sdk/loopx'), join(output, 'loopx'), { recursive: true });
  if (!await exists(join(output, 'acceptance.json')) && await exists(join(output, 'sdk/result.json'))) {
    const result = JSON.parse(await readFile(join(output, 'sdk/result.json'), 'utf8'));
    const validation = result.observation?.validation;
    await writeFile(join(output, 'acceptance.json'), JSON.stringify({ accepted: Boolean(candidate) && ['progress', 'completed'].includes(result.status) && validation?.status === 'passed' && state?.pending_effect_ids?.length === 0,
      candidate_sha256: candidate?.identity.sha256 ?? null,
      pending_effect_ids: state?.pending_effect_ids ?? null,
      provenance: 'sdk/result.json#observation.validation', original_validation: validation ?? null,
      scope: 'Original LoopX independent validator; not a new evaluation' }, null, 2) + '\n');
  }
  await mkdir(join(output, 'evaluations'), { recursive: true });
  await writeFile(join(output, 'evaluations/index.json'), JSON.stringify({ original_paths: names.filter(x => /evaluation|receipt|rsi-private/.test(x)),
    device_logs: names.filter(x => /\.(log|out|err)$/.test(x)) }, null, 2) + '\n');
  // A workspace may contain a later, unmeasured draft. Export the frozen identity separately.
  if (candidate) {
    await mkdir(join(output, 'candidate'), { recursive: true });
    for (const [name, bytes] of [['solution.py', candidate.bytes],
      ['identity.json', Buffer.from(JSON.stringify(candidate.identity, null, 2) + '\n')]]) {
      const target = join(output, 'candidate', name);
      if (await exists(target)) {
        if (!(await readFile(target)).equals(bytes)) throw new Error(`Candidate export conflicts with original ${name}`);
      } else await writeFile(target, bytes, { flag: 'wx' });
    }
    if (!await exists(join(output, 'workspace/solution.py'))) {
      await mkdir(join(output, 'workspace'), { recursive: true });
      await writeFile(join(output, 'workspace/solution.py'), candidate.bytes, { flag: 'wx' });
    }
  }
  const original = names.map(path => ({ path }));
  await writeFile(join(output, 'archive-provenance.json'), JSON.stringify({ source, original_files: original, original_paths_preserved: true }, null, 2) + '\n');
  return indexRun(output, { html });
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    const args = process.argv.slice(2);
    if (args.length !== 4 || args[0] !== '--input' || args[2] !== '--output' || !isAbsolute(args[1]) || !isAbsolute(args[3])) throw new Error('node export.mjs --input ABS_COMPLETED_RUN --output NEW_ABS');
    const result = await exportRun(args[1], args[3]);
    process.stdout.write(JSON.stringify({ files: result.files.length, pi_sessions: result.indices.pi_sessions.length, output: args[3] }) + '\n');
  } catch (error) { process.stderr.write(error.message + '\n'); process.exitCode = 1; }
}
