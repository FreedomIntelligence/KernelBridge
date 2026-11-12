// Local live-trace server: tails a session Journal read-only and streams the
// projected trace to the vendored Lattice viewer over Server-Sent Events.
import { createServer, type IncomingMessage, type ServerResponse } from 'node:http';
import { open, readFile, stat } from 'node:fs/promises';
import { watch, type FSWatcher } from 'node:fs';
import { randomBytes } from 'node:crypto';
import { spawn } from 'node:child_process';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { parseRows, projectTrace, type Trace } from './journal-trace.ts';

const VIEWER = fileURLToPath(new URL('./lattice-viewer.html', import.meta.url));
const ADAPTER = fileURLToPath(new URL('./live-adapter.js', import.meta.url));
// The vendored viewer boots with these two statements; the adapter must run first.
const BOOT = '\nloadSession(DS_INV);\nreadHash();\n';
// Demo-only text and context in the vendored viewer, replaced for live sessions.
// Each anchor must occur exactly once, so a changed viewer fails loudly.
const TRANSFORMS: [string, string][] = [
  ["  if (k === MAIN && DS.id === 'invoice') L =", "  if (DS.live) L = DS.baseCtx(k).map(function(b){ return newSeg(b[0], b[1], b[2], null, k).id; });\n  else if (k === MAIN && DS.id === 'invoice') L ="],
  ["'<span class=\"hint\">示例会话 · 分支 '", "'<span class=\"hint\">' + (DS && DS.live ? '实时会话' : '示例会话') + ' · 分支 '"],
  ['<p class="ov-k">三个分支 · 点选切换</p>', '<p class="ov-k">\' + (DS.live ? \'会话\' : \'三个分支 · 点选切换\') + \'</p>'],
  ["var cfg = '<div class=\"cfg\"><b>Harness · DSH standard</b>", "var cfg = DS.live ? DS.configHTML() : '<div class=\"cfg\"><b>Harness · DSH standard</b>"],
  ['<button class="btn" type="button" id="actFork">从这一步 fork</button>', "' + (DS.live ? '' : '<button class=\"btn\" type=\"button\" id=\"actFork\">从这一步 fork</button>') + '"],
];

/** Journal of an application session, resolved from its host directory (registry/<id>). */
export async function journalOf(hostDirectory: string): Promise<string> {
  const descriptor = JSON.parse(await readFile(join(hostDirectory, 'descriptor.json'), 'utf8'));
  const config = JSON.parse(await readFile(descriptor.config, 'utf8'));
  return join(config.state_dir, 'events.jsonl');
}

export async function viewerPage(trace: Trace): Promise<string> {
  const [html, adapter] = await Promise.all([readFile(VIEWER, 'utf8'), readFile(ADAPTER, 'utf8')]);
  const at = html.indexOf(BOOT); const script = html.indexOf('<script>');
  if (at < 0 || html.indexOf(BOOT, at + 1) >= 0 || script < 0 || script > at) throw new Error('Unsupported Lattice viewer: boot sequence not found exactly once');
  const data = JSON.stringify(trace).replace(/</g, '\\u003c');
  let body = html.slice(script, at);
  for (const [anchor, replacement] of TRANSFORMS) {
    const first = body.indexOf(anchor);
    if (first < 0 || body.indexOf(anchor, first + 1) >= 0) throw new Error('Unsupported Lattice viewer: transform anchor not found exactly once: ' + anchor.slice(0, 60));
    body = body.slice(0, first) + replacement + body.slice(first + anchor.length);
  }
  // Initial data precedes the viewer script; the adapter runs inside it, just before boot.
  return html.slice(0, script).replace(/<title>[^<]*<\/title>/, '<title>KernelBridge · 实时轨迹</title>')
    + `<script type="application/json" id="kb-trace">${data}</script>\n`
    + body + '\n/* kernelbridge live adapter */\n' + adapter + html.slice(at);
}

export interface TraceServer { url: string; port: number; close(): Promise<void> }

export async function startTraceServer(options: { journal: string; port?: number; host?: string; title?: string }): Promise<TraceServer> {
  const token = randomBytes(16).toString('hex');
  const host = options.host ?? '127.0.0.1';
  let rows: unknown[] = []; let offset = 0; let rest = ''; let trace: Trace = projectTrace([], { live: true, title: options.title });
  let encoded = JSON.stringify(trace); let version = 0;
  const clients = new Set<ServerResponse>();
  let reading = false; let dirty = false; let timer: NodeJS.Timeout | undefined;

  const publish = () => {
    const next = projectTrace(rows, { live: true, title: options.title });
    const body = JSON.stringify(next);
    if (body === encoded) return;
    trace = next; encoded = body; version++;
    for (const client of clients) client.write(`event: trace\nid: ${version}\ndata: ${body}\n\n`);
  };
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
      publish();
    } finally { reading = false; }
  };
  await read();
  let watcher: FSWatcher | undefined;
  try { watcher = watch(options.journal, () => { void read(); }); } catch { /* polling below still follows the Journal */ }
  // Polling covers platforms without reliable file events and advances running steps.
  timer = setInterval(() => { void read().then(() => { if (trace.session.status === 'running') publish(); }); }, 1000);

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
      if (route === '') return send(200, 'text/html; charset=utf-8', await viewerPage(trace));
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
  const port = (server.address() as { port: number }).port;
  return {
    url: `http://127.0.0.1:${port}/${token}/#invoice--timeline`, port,
    close: async () => {
      clearInterval(timer); watcher?.close();
      for (const client of clients) client.end();
      await new Promise<void>(accept => server.close(() => accept()));
    },
  };
}

/** Opens a URL in the user's browser without waiting for it. */
export function openBrowser(url: string): boolean {
  if (process.env.KERNELBRIDGE_NO_BROWSER === '1') return false;
  const command = process.platform === 'darwin' ? ['open', url] : process.platform === 'win32' ? ['cmd', '/c', 'start', '', url] : ['xdg-open', url];
  try { spawn(command[0], command.slice(1), { stdio: 'ignore', detached: true }).on('error', () => {}).unref(); return true; } catch { return false; }
}
