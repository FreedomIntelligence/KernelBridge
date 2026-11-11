import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { startApplication, attachApplication } from '../application/host.mjs';

/** Persistent product API shared by TUI, CLI and SDK. dispose detaches this client. */
export class ApplicationSession {
  constructor(client) {
    this.client = client;
    this.applicationSessionId = client.applicationSessionId;
    // Qualification is reported separately from the presence of an implementation.
    this.capabilities = Object.freeze({ events: true, tokenStreaming: true, nativeFork: true, persistentNativeSession: true });
  }
  static async create(options) { return new ApplicationSession(await startApplication(options)); }
  static async attach(identity, options) { return new ApplicationSession(await attachApplication(identity, options)); }
  state() { return this.client.state(); }
  subscribe(listener) { return this.client.subscribe(listener); }
  observe(after = -1) { return this.client.observe(after); }
  prompt(text, options = {}) { return this.client.input(text, { ...options, kind: 'prompt', channel: 'sdk' }); }
  steer(text, options = {}) { return this.client.input(text, { ...options, kind: 'steer', channel: 'sdk' }); }
  followUp(text, options = {}) { return this.client.input(text, { ...options, kind: 'follow_up', channel: 'sdk' }); }
  pause() { return this.client.control('pause'); }
  resume() { return this.client.control('resume'); }
  interrupt() { return this.client.control('interrupt'); }
  clearGoal() { return this.client.control('clear'); }
  setGoal(objective, options = {}) { return this.client.control('set', { ...options, objective }); }
  onDetach(value) { return this.client.control('on-detach', { value }); }
  command(text, options = {}) { return this.client.input(text, { ...options, kind: 'control', channel: 'sdk' }); }
  fork(entryId) { return this.command(entryId ? `/fork ${entryId}` : '/clone'); }
  async run() {
    const { ticket, worker_id: workerId } = await this.client.call('ticket', { mode: 'headless' });
    const worker = spawn(process.execPath, [fileURLToPath(new URL('../application/worker.mjs', import.meta.url)),
      '--host', this.client.directory, '--ticket', ticket, '--headless'], {
      stdio: ['ignore', 'ignore', 'pipe'], env: { PATH: process.env.PATH ?? '/usr/bin:/bin', LANG: 'C.UTF-8', PI_OFFLINE: '1' },
    });
    let error = '';
    worker.stderr.on('data', bytes => { error = (error + bytes).slice(-16384); });
    await new Promise((accept, reject) => {
      worker.once('error', reject);
      worker.once('exit', code => code === 0 ? accept() : reject(new Error(`Headless worker exited ${code}: ${error}`)));
    });
    const deadline = Date.now() + 30000;
    let state;
    do {
      state = await this.state();
      if (!state.lease || state.lease.worker_id !== workerId) return state;
      await new Promise(resolve => setTimeout(resolve, 50));
    } while (Date.now() < deadline);
    throw new Error('Worker exited but its original lease is still settling; inspect the session before running again');
  }
  async *events(after = -1) {
    const queue = [];
    let wake, failure;
    const unsubscribe = this.subscribe(event => {
      if (queue.length >= 1024) failure = new Error('Slow observer disconnected; resume from the original Journal cursor');
      else queue.push(event);
      wake?.();
    });
    try {
      let cursor = after;
      do {
        const page = await this.observe(cursor);
        yield { type: 'journal', ...page };
        if (page.next_cursor === null) break;
        cursor = page.next_cursor;
      } while (!this.client.closed);
      while (!this.client.closed) {
        if (failure) throw failure;
        if (queue.length) yield queue.shift();
        else await new Promise(resolve => { wake = resolve; });
      }
    } finally { unsubscribe(); }
  }
  dispose() { this.client.close(); }
}
