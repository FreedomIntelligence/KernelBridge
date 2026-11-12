// pi-lattice-trace: /trace opens this Pi session's live trajectory in the browser.
//
// The viewer reads the session branch from ctx.sessionManager (read-only) and
// the live state Pi has not persisted yet: executing tools and a streaming reply.
// One server per Pi process survives /new, /resume and /fork, so an open page
// follows the new session; it stops when Pi quits.
import type { ExtensionAPI, ExtensionContext } from '@earendil-works/pi-coding-agent';
import { openBrowser, serveTrace, type TraceServer } from '../src/server.ts';
import { exportTrace } from '../src/otlp.ts';
import { projectSession, type RunningTool } from '../src/session-trace.ts';
import type { Trace } from '../src/trace-core.ts';

interface SharedState {
  server?: Promise<TraceServer>;
  source: () => Trace;
  running: Map<string, RunningTool>;
  streamingSince: number | null;
}

const KEY = Symbol.for('pi-lattice-trace/state/v1');
const shared: SharedState = ((globalThis as any)[KEY] ??= {
  source: () => projectSession([], {}), running: new Map(), streamingSince: null,
});

const USAGE = '用法：/trace [--no-open] [--offline] [--port N] [--otlp]';

function parseArgs(raw: string) {
  const words = raw.trim().split(/\s+/).filter(Boolean);
  const options = { open: true, offline: false, otlp: false, port: 0, error: '' };
  for (let i = 0; i < words.length; i++) {
    const word = words[i];
    if (word === '--no-open') options.open = false;
    else if (word === '--offline') options.offline = true;
    else if (word === '--otlp') options.otlp = true;
    else if (word === '--port') {
      const port = Number(words[++i]);
      if (!Number.isInteger(port) || port < 0 || port > 65535) options.error = '--port 需要 0–65535 的整数';
      else options.port = port;
    } else options.error = '未知参数：' + word;
  }
  return options;
}

export default function latticeTrace(pi: ExtensionAPI) {
  let timer: ReturnType<typeof setTimeout> | undefined;

  // Binds the trace to this context's session. A captured context is only used
  // until session_shutdown, after which the last projection is kept frozen.
  const bind = (ctx: ExtensionContext) => {
    const sessions = ctx.sessionManager;
    shared.source = () => projectSession(sessions.getBranch(), {
      header: sessions.getHeader(), running: shared.running, streamingSince: shared.streamingSince,
      model: ctx.model ? { provider: ctx.model.provider, id: ctx.model.id, contextWindow: ctx.model.contextWindow } : null,
      thinkingLevel: ctx.thinkingLevel ?? null, sessionName: sessions.getSessionName() ?? null,
    });
  };
  // Pi appends an entry after message_end handlers return; publish on the next tick.
  const refresh = (ctx: ExtensionContext) => {
    bind(ctx);
    if (!shared.server) return;
    clearTimeout(timer);
    timer = setTimeout(() => { void shared.server?.then(server => server.setSource(shared.source)); }, 100);
  };

  pi.on('session_start', (_event, ctx) => { shared.running.clear(); shared.streamingSince = null; refresh(ctx); });
  pi.on('message_start', (event, ctx) => {
    if (event.message?.role === 'assistant') { shared.streamingSince = (event.message as any).timestamp ?? Date.now(); refresh(ctx); }
  });
  pi.on('message_end', (event, ctx) => {
    const message = event.message as any;
    if (message?.role === 'assistant') shared.streamingSince = null;
    if (message?.role === 'toolResult') shared.running.delete(message.toolCallId);
    refresh(ctx);
  });
  pi.on('tool_execution_start', (event, ctx) => { shared.running.set(event.toolCallId, { tool: event.toolName, args: event.args, startedAt: Date.now() }); refresh(ctx); });
  pi.on('tool_execution_end', (_event, ctx) => refresh(ctx));
  pi.on('agent_end', (_event, ctx) => { shared.running.clear(); shared.streamingSince = null; refresh(ctx); });
  for (const name of ['turn_end', 'session_compact', 'session_tree', 'session_info_changed', 'model_select', 'thinking_level_select'] as const)
    pi.on(name as any, (_event: unknown, ctx: ExtensionContext) => refresh(ctx));

  pi.on('session_shutdown', async event => {
    clearTimeout(timer);
    let last: Trace | undefined;
    try { last = shared.source(); } catch { /* the session is already gone */ }
    if (last) { const frozen = last; shared.source = () => frozen; }
    if (event.reason === 'quit' && shared.server) {
      const server = shared.server; shared.server = undefined;
      await (await server).close().catch(() => {});
    }
  });

  pi.registerCommand('trace', {
    description: '在浏览器中打开实时轨迹（Lattice Trace）',
    handler: async (args, ctx) => {
      const options = parseArgs(args ?? '');
      if (options.error) { ctx.ui.notify(options.error + '\n' + USAGE, 'error'); return; }
      bind(ctx);
      if (options.otlp) {
        // One-shot export to OTEL_EXPORTER_OTLP_ENDPOINT; content stays out unless the capture variables select it.
        try {
          const result = await exportTrace(shared.source(), { service: 'pi' });
          ctx.ui.notify(`已导出 ${result.spans} 个 span → ${result.endpoint}`, 'info');
        } catch (error) { ctx.ui.notify(error instanceof Error ? error.message : String(error), 'error'); }
        return;
      }
      const existing = shared.server;
      shared.server ??= serveTrace({ snapshot: () => shared.source(), port: options.port, unref: true,
        page: { title: 'Pi · 实时轨迹', offline: options.offline } });
      let server: TraceServer;
      try { server = await shared.server; }
      catch (error) { shared.server = undefined; ctx.ui.notify('无法启动轨迹服务：' + (error instanceof Error ? error.message : String(error)), 'error'); return; }
      if (existing) server.setSource(shared.source);
      const remote = Boolean(process.env.SSH_CONNECTION || process.env.SSH_TTY);
      const opened = options.open && !remote && process.env.PI_LATTICE_NO_BROWSER !== '1' && openBrowser(server.url);
      const lines = [`实时轨迹：${server.url}`];
      if (existing && (options.port || options.offline)) lines.push('服务已在运行，沿用原来的端口和设置。');
      if (remote) lines.push(`远程会话：在本机运行 ssh -L ${server.port}:127.0.0.1:${server.port} <主机>，再打开上面的地址。`);
      else if (!opened) lines.push('在浏览器中打开上面的地址。');
      lines.push('地址含访问令牌，等同于会话内容的读取权限，不要分享。');
      ctx.ui.notify(lines.join('\n'), 'info');
    },
  });
}
