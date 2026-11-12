// lattice-trace SESSION.jsonl [--port N] [--no-open] [--offline] [--otlp]
// Opens a saved (or still running) Pi session file in the Lattice viewer and
// follows appends. The branch shown is the one Pi resumes: from the last entry
// back to the root through parentId.
import { readFile, stat } from 'node:fs/promises';
import { watch, type FSWatcher } from 'node:fs';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import type { Trace } from '../src/trace-core.ts';
import { projectSession } from '../src/session-trace.ts';
import { openBrowser, serveTrace } from '../src/server.ts';
import { exportTrace } from '../src/otlp.ts';
import { childSessionEntries, sessionBranch } from '../src/session-file.ts';

export { sessionBranch };

export async function traceFile(path: string): Promise<Trace> {
  const { header, entries, tree } = sessionBranch(await readFile(path, 'utf8'));
  const name = [...entries].reverse().find(entry => entry.type === 'session_info' && entry.name)?.name ?? null;
  return projectSession(entries, { header, sessionName: name, live: true, tree, childEntries: childSessionEntries });
}

const USAGE = 'usage: lattice-trace SESSION.jsonl [--port N] [--no-open] [--offline] [--otlp]';

export async function main(argv: string[]): Promise<number> {
  const file = argv.find(arg => !arg.startsWith('--') && argv[argv.indexOf(arg) - 1] !== '--port');
  if (!file || argv.includes('--help')) { process.stdout.write(USAGE + '\n'); return file ? 0 : 2; }
  const path = resolve(file);
  if (argv.includes('--otlp')) {
    const result = await exportTrace(await traceFile(path), { service: 'pi' });
    process.stdout.write(`已导出 ${result.spans} 个 span → ${result.endpoint}\n`);
    return 0;
  }
  const portIndex = argv.indexOf('--port'); const port = portIndex >= 0 ? Number(argv[portIndex + 1]) : 0;
  if (!Number.isInteger(port) || port < 0 || port > 65535) { process.stderr.write('--port 需要 0–65535 的整数\n'); return 2; }
  let trace = await traceFile(path);
  const server = await serveTrace({ snapshot: () => trace, port, page: { title: 'Pi · 会话轨迹', offline: argv.includes('--offline') } });
  let size = (await stat(path)).size; let busy = false;
  const refresh = async () => {
    if (busy) return; busy = true;
    try {
      const now = (await stat(path).catch(() => null))?.size;
      if (now !== undefined && now !== size) { size = now; trace = await traceFile(path); server.publish(); }
    } finally { busy = false; }
  };
  let watcher: FSWatcher | undefined;
  try { watcher = watch(path, () => { void refresh(); }); } catch { /* polling below */ }
  const timer = setInterval(() => { void refresh(); }, 1000);
  process.stdout.write(`会话轨迹：${server.url}\n文件：${path}\n按 Ctrl-C 停止。\n`);
  if (!argv.includes('--no-open') && process.env.PI_LATTICE_NO_BROWSER !== '1' && !process.env.SSH_CONNECTION) openBrowser(server.url);
  await new Promise(done => { process.once('SIGINT', done); process.once('SIGTERM', done); });
  clearInterval(timer); watcher?.close(); await server.close();
  return 0;
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  main(process.argv.slice(2)).then(code => { process.exitCode = code; }, error => { process.stderr.write(String(error?.message ?? error) + '\n'); process.exitCode = 1; });
}
