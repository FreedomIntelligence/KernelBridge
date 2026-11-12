// Startup overlap for the interactive CLI. Kept free of heavy imports: main.ts loads it
// first so the interface process (Pi, about 0.8 s of module loading) starts while the CLI
// and the Python host start, and a terminal shows a notice within a few hundred ms.
import { spawn, spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

/** Commands that end in the interactive interface (see applicationMain). */
export function startsInterface(args) {
  return !['trace', 'sessions', 'session'].includes(args[0]) && !args.includes('--help') && !args.includes('-h')
    && Boolean(process.stdin.isTTY && process.stdout.isTTY);
}

/** The terminal between launch and the interface's first paint.

    A dim one-line notice, erased before the interface draws (never left in scrollback).
    Keys typed meanwhile wait unechoed for the interface: echo, line buffering and CR→NL
    translation are off, so a typed command keeps its Enter instead of leaving "/quit" in
    scrollback and a newline in the editor. Signals stay on (Ctrl+C still stops startup).
    restore() puts back the saved mode; the interface restores to the startup mode when it
    exits, so the CLI restores last. Only on a terminal. */
export function startupStatus() {
  if (!process.stdin.isTTY || !process.stdout.isTTY) return { clear() {}, restore() {} };
  const stty = args => spawnSync('stty', args, { stdio: ['inherit', 'pipe', 'ignore'], encoding: 'utf8' });
  const saved = stty(['-g']);
  const mode = saved.status === 0 ? saved.stdout.trim() : null;
  if (mode) stty(['-echo', '-icanon', '-icrnl', 'min', '1', 'time', '0']);
  let shown = false, restored = false;
  const restore = () => { if (mode && !restored) { restored = true; stty([mode]); } };
  const interrupted = signal => { restore(); process.kill(process.pid, signal); };
  const onInterrupt = () => interrupted('SIGINT'), onTerminate = () => interrupted('SIGTERM');
  if (mode) { process.once('SIGINT', onInterrupt); process.once('SIGTERM', onTerminate); }
  if (process.env.KERNELBRIDGE_UI_DETERMINISTIC !== '1') {
    process.stdout.write('\x1b[2mKernelBridge 正在启动…\x1b[22m');
    shown = true;
  }
  return {
    /** Before the interface (or a CLI picker) draws. */
    clear() { if (shown) { shown = false; process.stdout.write('\r\x1b[2K'); } },
    /** When the CLI is done with the terminal. */
    restore() { this.clear(); process.off('SIGINT', onInterrupt); process.off('SIGTERM', onTerminate); restore(); },
  };
}

/** Spawn the interface process before it is needed. It imports its modules, reports
    `prewarmed`, and waits; `start` hands over the host directory and one-use ticket. */
export function prewarmWorker() {
  // The interface process receives no model/device secrets: only display and locale variables.
  const env = { PATH: process.env.PATH ?? '/usr/bin:/bin', LANG: process.env.LANG ?? 'en_US.UTF-8', TERM: process.env.TERM ?? 'xterm-256color',
    KERNELBRIDGE_HOME: process.env.KERNELBRIDGE_HOME, COLORTERM: process.env.COLORTERM, TERM_PROGRAM: process.env.TERM_PROGRAM, PI_OFFLINE: '1',
    KERNELBRIDGE_DEMO: process.env.KERNELBRIDGE_DEMO === '1' ? '1' : undefined };
  const child = spawn(process.execPath, [fileURLToPath(new URL('./worker.mjs', import.meta.url)), '--prewarm'], {
    stdio: ['inherit', 'inherit', 'inherit', 'ipc'], env: Object.fromEntries(Object.entries(env).filter(([, value]) => value !== undefined)),
  });
  const ready = new Promise((resolve, reject) => {
    const exit = code => reject(new Error(`Interface process exited during startup (${code})`));
    child.once('error', reject);
    child.once('exit', exit);
    child.once('message', message => {
      child.off('exit', exit);
      if (message?.type === 'prewarmed') resolve(); else reject(new Error('Interface process sent an unexpected startup message'));
    });
  });
  ready.catch(() => {});
  return {
    /** message: { host, ticket, environment: { KERNELBRIDGE_SESSIONS, KERNELBRIDGE_TUI_MODE } } */
    async start(message, beforeHandOver) {
      await ready;
      beforeHandOver?.();
      child.send({ type: 'start', ...message });
      return child;
    },
    cancel() { if (child.exitCode === null && child.signalCode === null) child.kill(); },
  };
}
