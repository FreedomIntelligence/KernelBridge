/** Restore verified original Pi conversation bytes and complete run evidence. */
import { writeFile, mkdir } from 'node:fs/promises';
import { dirname, join, resolve, isAbsolute } from 'node:path';
import { fileURLToPath } from 'node:url';
import { SessionManager, parseSessionEntries } from '../../source/apps/agent/node_modules/@earendil-works/pi-coding-agent/dist/index.js';
import { inspectNativeSessionFile } from '../../source/apps/agent/src/replay/native-session.ts';
import { verifyArchive, digest } from './archive.mjs';

export async function restoreSession(input, output) {
  const { manifest, manifestBytes, files } = await verifyArchive(input);
  output = resolve(output);
  await mkdir(output, { mode: 0o700 });
  for (const [name, data] of [...files, ['manifest.json', manifestBytes]]) {
    await mkdir(dirname(join(output, name)), { recursive: true });
    await writeFile(join(output, name), data, { flag: 'wx', mode: 0o600 });
  }
  const checks = [];
  for (const name of manifest.indices.pi_sessions) {
    const restored = await inspectNativeSessionFile(join(output, name));
    if (restored.digest !== digest(files.get(name))) throw new Error('Restored Pi session changed');
    const entries = parseSessionEntries(files.get(name).toString('utf8'));
    const original = SessionManager.inMemory(entries[0].cwd, undefined, entries);
    const context = original.buildSessionContext();
    checks.push({ session: name, session_id: restored.sessionId, leaf_id: restored.leafId,
      entries: original.getEntries().length, messages: context.messages.length,
      context_sha256: digest(JSON.stringify(context)), verified: true });
  }
  const primary = manifest.indices.pi_sessions[0];
  await writeFile(join(output, 'session.jsonl'), files.get(primary), { flag: 'wx', mode: 0o600 });
  if (files.has(primary + '.html')) await writeFile(join(output, 'session.html'), files.get(primary + '.html'), { flag: 'wx', mode: 0o600 });
  const report = { restored: true, files_verified: manifest.files.length, sessions: checks,
    archive_manifest_sha256: digest(manifestBytes), primary_session: join(output, primary),
    continue_argv: [process.execPath, fileURLToPath(new URL('./continue-run.mjs', import.meta.url)), '--restored', output],
    execution: 'Original Pi conversation restored; continue with a local profile and a new execution output directory' };
  await writeFile(join(output, 'restore.json'), JSON.stringify(report, null, 2) + '\n', { flag: 'wx', mode: 0o600 });
  return report;
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    const args = process.argv.slice(2);
    if (args.length !== 4 || args[0] !== '--run' || args[2] !== '--output' || !isAbsolute(args[1]) || !isAbsolute(args[3])) throw new Error('node restore-session.mjs --run ABS_ARCHIVED_RUN --output NEW_ABS');
    console.log(JSON.stringify(await restoreSession(args[1], args[3])));
  } catch (error) { console.error(error.message); process.exitCode = 1; }
}
