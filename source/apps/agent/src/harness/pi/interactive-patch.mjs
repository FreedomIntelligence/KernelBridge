import { createHash } from 'node:crypto';
import { readFile, writeFile, rename } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const digest = text => createHash('sha256').update(text).digest('hex');
const patchDirectory = fileURLToPath(new URL('../../../patches/', import.meta.url));

/** Apply an exact, reviewed unified diff to an adjacent module; npm source is untouched. */
export async function loadInteractiveMode() {
  const entry = fileURLToPath(import.meta.resolve('@earendil-works/pi-coding-agent'));
  const root = dirname(dirname(entry));
  const metadata = JSON.parse(await readFile(join(patchDirectory, 'pi-coding-agent-1.5.0-host-actions.json'), 'utf8'));
  const manifest = JSON.parse(await readFile(join(root, 'package.json'), 'utf8'));
  const sourcePath = join(root, 'dist/modes/interactive/interactive-mode.js');
  const source = await readFile(sourcePath, 'utf8');
  const patch = await readFile(join(patchDirectory, 'pi-coding-agent-1.5.0-host-actions.patch'), 'utf8');
  if (manifest.version !== metadata.version || digest(source) !== metadata.source_sha256 || digest(patch) !== metadata.patch_sha256) {
    throw new Error('Pi version/source differs from the reviewed host-actions patch; interactive startup refused');
  }
  const lines = source.split('\n');
  const result = []; let cursor = 0; let active = false;
  for (const line of patch.split('\n')) {
    if (line.startsWith('@@ ')) {
      const match = /^@@ -(\d+)(?:,\d+)? \+\d+(?:,\d+)? @@/.exec(line);
      if (!match) throw new Error('Invalid versioned patch hunk');
      const start = Number(match[1]) - 1;
      result.push(...lines.slice(cursor, start)); cursor = start; active = true;
    } else if (active && line.startsWith(' ')) {
      if (lines[cursor] !== line.slice(1)) throw new Error('Pi patch context mismatch');
      result.push(lines[cursor++]);
    } else if (active && line.startsWith('-')) {
      if (lines[cursor++] !== line.slice(1)) throw new Error('Pi patch deletion mismatch');
    } else if (active && line.startsWith('+')) result.push(line.slice(1));
  }
  result.push(...lines.slice(cursor));
  const patched = result.join('\n');
  if (digest(patched) !== metadata.patched_sha256) throw new Error('Pi patched module digest mismatch');
  const target = join(dirname(sourcePath), 'interactive-mode.host.js');
  const temporary = `${target}.${process.pid}.tmp`;
  await writeFile(temporary, patched, { mode: 0o600 });
  await rename(temporary, target);
  return { InteractiveMode: (await import(pathToFileURL(target).href)).InteractiveMode, metadata };
}
