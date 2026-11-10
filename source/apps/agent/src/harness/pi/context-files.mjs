/** Explicit Pi project instructions from the admitted task workspace. */
import { lstatSync, readFileSync, realpathSync } from 'node:fs';
import { join } from 'node:path';

export function projectInstructions(cwd) {
  const agentsFiles = [];
  for (const name of ['AGENTS.md', 'AGENTS.task.md']) {
    const path = join(cwd, name);
    let info;
    try { info = lstatSync(path); }
    catch (error) { if (error.code === 'ENOENT') continue; throw error; }
    if (!info.isFile() || info.isSymbolicLink() || realpathSync(path) !== path || info.size > 131072) throw new Error('Invalid admitted instruction file: ' + name);
    const content = readFileSync(path, 'utf8');
    if (Buffer.byteLength(content) > 131072) throw new Error('Instruction file exceeds 128 KiB');
    agentsFiles.push({ path, content });
  }
  return { agentsFiles };
}
