// KernelBridge live trace: tails a session Journal read-only and serves its
// projection with the shared Lattice Trace server (source/packages/pi-lattice-trace).
import { open, readFile, stat } from 'node:fs/promises';
import { watch, type FSWatcher } from 'node:fs';
import { join } from 'node:path';
import { parseRows, projectTrace } from './journal-trace.ts';
import { serveTrace, openBrowser as openUrl, type TraceServer } from '../../../../packages/pi-lattice-trace/src/server.ts';

export { viewerPage } from '../../../../packages/pi-lattice-trace/src/viewer-page.ts';
export type { TraceServer };

/** Journal of an application session, resolved from its host directory (registry/<id>). */
export async function journalOf(hostDirectory: string): Promise<string> {
  const descriptor = JSON.parse(await readFile(join(hostDirectory, 'descriptor.json'), 'utf8'));
  const config = JSON.parse(await readFile(descriptor.config, 'utf8'));
  return join(config.state_dir, 'events.jsonl');
}

/** The full projection of a saved or running session's Journal, read once. */
export async function readTrace(journal: string, title?: string) {
  return projectTrace(parseRows(await readFile(journal, 'utf8')).rows, { live: false, title });
}

export async function startTraceServer(options: { journal: string; port?: number; host?: string; title?: string }): Promise<TraceServer> {
  let rows: unknown[] = []; let offset = 0; let rest = '';
  let reading = false; let dirty = false;
  const server = await serveTrace({ snapshot: () => projectTrace(rows, { live: true, title: options.title }), port: options.port, host: options.host });

  const read = async () => {
    if (reading) { dirty = true; return; }
    reading = true;
    try {
      do {
        dirty = false;
        const info = await stat(options.journal).catch(() => null);
        if (!info) continue;
        if (info.size < offset) { rows = []; offset = 0; rest = ''; }
        if (info.size === offset) continue;
        const handle = await open(options.journal, 'r');
        try {
          const buffer = Buffer.alloc(info.size - offset);
          const { bytesRead } = await handle.read(buffer, 0, buffer.length, offset);
          offset += bytesRead;
          const parsed = parseRows(rest + buffer.subarray(0, bytesRead).toString('utf8'));
          rest = parsed.rest; rows = rows.concat(parsed.rows);
        } finally { await handle.close(); }
      } while (dirty);
      server.publish();
    } finally { reading = false; }
  };
  await read();
  let watcher: FSWatcher | undefined;
  try { watcher = watch(options.journal, () => { void read(); }); } catch { /* polling below still follows the Journal */ }
  // Polling covers platforms without reliable file events; the server advances running steps.
  const timer = setInterval(() => { void read(); }, 1000);
  return { ...server, close: async () => { clearInterval(timer); watcher?.close(); await server.close(); } };
}

/** Opens a URL in the user's browser without waiting for it. */
export function openBrowser(url: string): boolean {
  if (process.env.KERNELBRIDGE_NO_BROWSER === '1') return false;
  return openUrl(url);
}
