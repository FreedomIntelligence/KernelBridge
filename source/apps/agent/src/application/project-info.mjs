import { execFile } from 'node:child_process';
import { promisify } from 'node:util';

/** Advisory Git identity; failures never prevent ordinary non-Git codegen. */
export async function projectInfo(cwd) {
  if (!cwd) return null;
  try {
    const run = args => promisify(execFile)('git', ['-C', cwd, ...args], { timeout: 2000, maxBuffer: 16384 });
    const [paths, symbolic] = await Promise.all([
      run(['rev-parse', '--path-format=absolute', '--show-toplevel', '--git-common-dir']),
      run(['symbolic-ref', '--quiet', '--short', 'HEAD']).catch(() => null),
    ]);
    const [root, common_directory] = paths.stdout.trim().split('\n');
    const branch = symbolic?.stdout.trim() || `detached:${(await run(['rev-parse', '--short', 'HEAD'])).stdout.trim()}`;
    return { root, common_directory, branch };
  } catch { return null; }
}
