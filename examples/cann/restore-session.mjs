/** Restore an archived Pi session and its complete run evidence into a new directory. */
import { readFile, writeFile, mkdir, copyFile, realpath } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { dirname, join, resolve, isAbsolute } from 'node:path';
import { fileURLToPath } from 'node:url';
import { SessionManager } from '../../source/apps/agent/node_modules/@earendil-works/pi-coding-agent/dist/index.js';
const hash = value => createHash('sha256').update(value).digest('hex');

export async function restoreSession(input, output) {
  input = await realpath(input); output = resolve(output);
  const manifest = JSON.parse(await readFile(join(input, 'manifest.json'), 'utf8'));
  if (manifest.schema_version !== 'kernelbridge.run-archive/v1') throw new Error('Expected a run archive manifest');
  for (const row of manifest.files) {
    if (isAbsolute(row.path) || row.path.split('/').includes('..')) throw new Error('Invalid archive path');
    const path = join(input, row.path);
    if (await realpath(path) !== path) throw new Error('Archive entry traverses a symlink');
    const data = await readFile(path);
    if (data.length !== row.bytes || hash(data) !== row.sha256) throw new Error('Archive hash mismatch: ' + row.path);
  }
  const sessions = manifest.indices.pi_sessions;
  if (!sessions.length || sessions.some(name => !manifest.files.some(row => row.path === name))) throw new Error('Missing indexed Pi session');
  await mkdir(output, { mode: 0o700 });
  for (const name of [...manifest.files.map(row => row.path), 'manifest.json']) {
    await mkdir(dirname(join(output, name)), { recursive: true });
    await copyFile(join(input, name), join(output, name));
  }
  const checks = [];
  for (const name of sessions) {
    const original = SessionManager.open(join(input, name));
    const restored = SessionManager.open(join(output, name));
    const before = original.buildSessionContext(); const after = restored.buildSessionContext();
    if (original.getSessionId() !== restored.getSessionId() || original.getLeafId() !== restored.getLeafId()
        || JSON.stringify(before) !== JSON.stringify(after)) throw new Error('Pi restored context differs');
    checks.push({ session: name, session_id: restored.getSessionId(), leaf_id: restored.getLeafId(),
      entries: restored.getEntries().length, messages: after.messages.length, context_sha256: hash(JSON.stringify(after)), verified: true });
  }
  const primary = join(output, sessions[0]);
  await copyFile(primary, join(output, 'session.jsonl'));
  if (manifest.files.some(row => row.path === sessions[0] + '.html')) await copyFile(primary + '.html', join(output, 'session.html'));
  const report = { restored: true, files_verified: manifest.files.length, sessions: checks,
    primary_session: primary, pi_open_argv: [process.execPath, fileURLToPath(new URL('../../source/apps/agent/node_modules/@earendil-works/pi-coding-agent/dist/cli.js', import.meta.url)), '--session', primary],
    execution: 'Pi SessionManager restored the original conversation and active branch; no new model turn was started' };
  await writeFile(join(output, 'restore.json'), JSON.stringify(report, null, 2) + '\n');
  return report;
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    const args = process.argv.slice(2);
    if (args.length !== 4 || args[0] !== '--run' || args[2] !== '--output' || !isAbsolute(args[1]) || !isAbsolute(args[3])) throw new Error('node restore-session.mjs --run ABS_ARCHIVED_RUN --output NEW_ABS');
    console.log(JSON.stringify(await restoreSession(args[1], args[3])));
  } catch (error) { console.error(error.message); process.exitCode = 1; }
}
