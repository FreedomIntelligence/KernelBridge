/** Explicit Pi project instructions from the admitted task workspace. */
import { lstatSync, readFileSync, realpathSync } from 'node:fs';
import { join } from 'node:path';
import { createHash } from 'node:crypto';

const NAMES = ['AGENTS.md', 'AGENTS.task.md'];

export function projectInstructions(cwd, admitted) {
  if (admitted !== undefined) {
    if (admitted?.schema_version !== 'kernelbridge.project-instructions/v1' || !Array.isArray(admitted.files)) {
      throw new Error('Invalid host instruction snapshot');
    }
    let previous = -1;
    const agentsFiles = admitted.files.map(file => {
      const index = NAMES.indexOf(file.path);
      if (index <= previous || typeof file.content !== 'string'
          || !Number.isInteger(file.bytes) || file.bytes !== Buffer.byteLength(file.content, 'utf8')
          || file.bytes > 131072
          || file.sha256 !== createHash('sha256').update(file.content, 'utf8').digest('hex')) {
        throw new Error('Invalid host instruction snapshot file');
      }
      previous = index;
      return { path: join(cwd, file.path), content: file.content };
    });
    return { agentsFiles };
  }
  // Compatibility for direct embedders without the composed host capability.
  const agentsFiles = [];
  for (const name of NAMES) {
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
