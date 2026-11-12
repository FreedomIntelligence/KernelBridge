import { realpath } from 'node:fs/promises';
import { applicationDirectory, sessions } from './host.mjs';
import { journalOf, openBrowser, startTraceServer } from '../trace/server.ts';

export const TRACE_HELP = `kernelagent trace [ID_OR_TITLE] [--all] [--port N] [--no-open]
  Serve the live trajectory viewer (Lattice Trace) for a session on 127.0.0.1.
  Without an ID it follows this project's latest session; --all searches every project.
  The viewer tails the session Journal read-only and updates as the session runs.
`;

/** Resolves one saved session the same way `resume` does. */
async function selectSession(query, all) {
  const cwd = await realpath(process.cwd());
  const available = (await sessions()).filter(item => all || query || item.cwd === cwd);
  if (!query) return available[0] ?? null;
  const exact = available.filter(item => item.application_session_id === query || item.title === query);
  const found = exact.length ? exact : available.filter(item => item.application_session_id.startsWith(query) || item.application_session_id.startsWith('application-' + query));
  if (found.length > 1) throw new Error('会话名称不唯一，请使用完整 ID。');
  return found[0] ?? null;
}

export async function traceCommand(args) {
  if (args.includes('--help') || args.includes('-h')) { process.stdout.write(TRACE_HELP); return 0; }
  const portIndex = args.indexOf('--port');
  const port = portIndex >= 0 ? Number(args[portIndex + 1]) : 0;
  if (!Number.isInteger(port) || port < 0 || port > 65535) throw new Error('--port 需要 0–65535 的整数');
  const query = args.find((value, index) => !value.startsWith('--') && index !== portIndex + 1);
  const selected = await selectSession(query, args.includes('--all'));
  if (!selected) throw new Error(query ? '没有匹配的会话；用 kernelagent sessions 查看全部。' : '当前项目没有会话；先运行 kernelagent，或传入会话 ID。');
  const server = await startTraceServer({ journal: await journalOf(await applicationDirectory(selected.application_session_id)), port, title: selected.title });
  process.stdout.write(`实时轨迹：${server.url}\n会话：${selected.title ?? selected.application_session_id}\n按 Ctrl-C 停止。\n`);
  if (!args.includes('--no-open')) openBrowser(server.url);
  await new Promise(resolve => { process.once('SIGINT', resolve); process.once('SIGTERM', resolve); });
  await server.close();
  return 0;
}

/** One viewer per interactive session, hosted by the trusted CLI process (not the TUI worker). */
export function traceHost(hostDirectory) {
  let server;
  return {
    async open() {
      server ??= await startTraceServer({ journal: await journalOf(hostDirectory) });
      return { url: server.url, opened: openBrowser(server.url) };
    },
    async close() { const current = server; server = undefined; await current?.close(); },
  };
}
