// Local live-trace server: serves the Lattice viewer for one trace source and
// pushes each changed snapshot over Server-Sent Events. The source is a pure
// snapshot function; callers decide when the data changed and call publish().
import { createServer, type IncomingMessage, type ServerResponse } from 'node:http';
import { randomBytes } from 'node:crypto';
import { spawn } from 'node:child_process';
import type { Trace } from './trace-core.ts';
import { viewerPage, type PageOptions } from './viewer-page.ts';

export interface TraceServer {
  url: string; port: number;
  /** Re-reads the snapshot and pushes it to open pages when it changed. */
  publish(): void;
  /** Replaces the trace source (for example after a session switch) and publishes it. */
  setSource(snapshot: () => Trace): void;
  close(): Promise<void>;
}

export interface ServeOptions { snapshot: () => Trace; port?: number; host?: string; page?: PageOptions; unref?: boolean }

export async function serveTrace(options: ServeOptions): Promise<TraceServer> {
  const token = randomBytes(16).toString('hex');
  const host = options.host ?? '127.0.0.1';
  let snapshot = options.snapshot;
  let trace: Trace = snapshot(); let encoded = JSON.stringify(trace); let version = 0;
  const clients = new Set<ServerResponse>();

  const publish = () => {
    let next: Trace;
    try { next = snapshot(); } catch (error) { console.error('[lattice-trace] projection failed', error); return; }
    const body = JSON.stringify(next);
    if (body === encoded) return;
    trace = next; encoded = body; version++;
    for (const client of clients) client.write(`event: trace\nid: ${version}\ndata: ${body}\n\n`);
  };
  // Running steps end at "now"; keep advancing them while anything runs.
  const timer = setInterval(() => { if (trace.session.status === 'running') publish(); }, 1000);
  timer.unref?.();

  const allowed = (request: IncomingMessage, port: number) => [`127.0.0.1:${port}`, `localhost:${port}`].includes(request.headers.host ?? '');
  const server = createServer(async (request, response) => {
    const port = (server.address() as { port: number }).port;
    const url = new URL(request.url ?? '/', `http://${host}`);
    const send = (status: number, type: string, body: string) => {
      response.writeHead(status, { 'Content-Type': type, 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff', 'Referrer-Policy': 'no-referrer' });
      response.end(body);
    };
    if (request.method !== 'GET' || !allowed(request, port) || !url.pathname.startsWith(`/${token}/`)) return send(404, 'text/plain; charset=utf-8', 'not found');
    const route = url.pathname.slice(token.length + 2);
    try {
      if (route === '') return send(200, 'text/html; charset=utf-8', await viewerPage(trace, options.page));
      if (route === 'trace.json') return send(200, 'application/json; charset=utf-8', encoded);
      if (route === 'events') {
        response.writeHead(200, { 'Content-Type': 'text/event-stream; charset=utf-8', 'Cache-Control': 'no-store', Connection: 'keep-alive', 'X-Accel-Buffering': 'no' });
        response.write(`retry: 2000\nevent: trace\nid: ${version}\ndata: ${encoded}\n\n`);
        clients.add(response);
        const ping = setInterval(() => response.write(': ping\n\n'), 15000);
        request.on('close', () => { clearInterval(ping); clients.delete(response); });
        return;
      }
      send(404, 'text/plain; charset=utf-8', 'not found');
    } catch (error) { send(500, 'text/plain; charset=utf-8', error instanceof Error ? error.message : String(error)); }
  });
  await new Promise<void>((accept, reject) => { server.once('error', reject); server.listen(options.port ?? 0, host, () => accept()); });
  // A host process that exits on its own (Pi print mode) must not be held open by the viewer.
  if (options.unref) server.unref();
  const port = (server.address() as { port: number }).port;
  return {
    url: `http://127.0.0.1:${port}/${token}/#invoice--timeline`, port, publish,
    setSource(next) { snapshot = next; publish(); },
    close: async () => {
      clearInterval(timer);
      for (const client of clients) client.end();
      await new Promise<void>(accept => server.close(() => accept()));
    },
  };
}

/** Opens a URL in the user's browser without waiting for it. */
export function openBrowser(url: string): boolean {
  const command = process.platform === 'darwin' ? ['open', url] : process.platform === 'win32' ? ['cmd', '/c', 'start', '', url] : ['xdg-open', url];
  try { spawn(command[0], command.slice(1), { stdio: 'ignore', detached: true }).on('error', () => {}).unref(); return true; } catch { return false; }
}
