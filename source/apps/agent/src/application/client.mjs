import { connect } from 'node:net';
import { lstat, readFile } from 'node:fs/promises';
import { join } from 'node:path';
import { randomUUID } from 'node:crypto';
import { frames } from '../clients/protocol.mjs';

/** Durable boundary operations queue behind the original finite effects.
 * snapshot also prepares fork/reset/clone transitions; select_session(path)
 * validates retained bytes and may restore source. Keep ordinary control
 * replies short while allowing these known operations to finish their fsyncs.
 * Effect/provider deadlines remain owned by WorkerLease and the executor.
 */
export function acknowledgementTimeout(method, params = {}) {
  return method === 'snapshot' || (method === 'select_session' && params.path) ? 300000 : 30000;
}

/** Multiplexed control connection. Lost replies are uncertain, never retried. */
export class ApplicationClient {
  constructor(socket) {
    this.socket = socket;
    this.pending = new Map();
    this.listeners = new Set();
    this.sequence = 0;
    this.closed = false;
    socket.on('error', error => this.fail(error));
    this.reader = this.read();
    void this.reader.catch(error => this.fail(error));
  }
  static async connect(directory, { timeoutMs = 10000 } = {}) {
    for (const [name, mode] of [['', 0o700], ['token', 0o600], ['host.sock', 0o600]]) {
      const info = await lstat(join(directory, name));
      if (info.isSymbolicLink() || (info.mode & 0o777) !== mode || info.uid !== process.getuid()) {
        throw new Error(`Unsafe application ${name || 'directory'} permissions`);
      }
    }
    const token = await readFile(join(directory, 'token'), 'utf8');
    const socket = connect(join(directory, 'host.sock'));
    const client = new ApplicationClient(socket);
    try {
      const hello = await client.call('authenticate', { token }, timeoutMs);
      client.applicationSessionId = hello.application_session_id;
      client.directory = directory;
      return client;
    } catch (error) { client.close(); throw error; }
  }
  async read() {
    for await (const message of frames(this.socket, 16 * 1024 * 1024)) {
      if (Object.hasOwn(message, 'id')) {
        const item = this.pending.get(message.id);
        if (!item) continue; // A timed-out acknowledgement cannot trigger execution.
        this.pending.delete(message.id);
        clearTimeout(item.timer);
        if (message.error) item.reject(new Error(`${message.error.name}: ${message.error.message}`));
        else item.resolve(message.result);
      } else {
        for (const listener of this.listeners) {
          try { listener(structuredClone(message)); } catch { /* observation cannot control execution */ }
        }
      }
    }
    this.fail(new Error('Application host disconnected; inspect original receipts'));
  }
  call(method, params = {}, timeoutMs = acknowledgementTimeout(method, params)) {
    if (this.closed) return Promise.reject(this.failure ?? new Error('Application connection is closed'));
    const id = ++this.sequence;
    const bytes = JSON.stringify({ id, method, params }) + '\n';
    if (Buffer.byteLength(bytes) > 16 * 1024 * 1024) return Promise.reject(new Error('Application frame exceeds 16 MiB'));
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        const error = new Error(`${method} acknowledgement timed out; outcome is unknown, retain the original input/call ID`);
        error.code = 'ACKNOWLEDGEMENT_TIMEOUT';
        error.method = method;
        error.requestId = id;
        error.original = Object.fromEntries(['input_id', 'call_id', 'pi_session_id', 'entry_id', 'operation']
          .filter(key => params[key] !== undefined).map(key => [key, params[key]]));
        reject(error);
      }, timeoutMs);
      this.pending.set(id, { resolve, reject, timer });
      this.socket.write(bytes, error => { if (error) this.fail(error); });
    });
  }
  subscribe(listener) {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }
  async observe(after = -1) { return this.call('subscribe', { after }); }
  async state() { return this.call('status'); }
  async input(text, { inputId = randomUUID(), kind = 'prompt', channel = 'sdk', initiator = 'user' } = {}) {
    return this.call('accept', { input_id: inputId, text, kind, channel, initiator });
  }
  async control(operation, values = {}) {
    return this.call('control', { input_id: randomUUID(), operation, channel: 'sdk', ...values });
  }
  fail(error) {
    if (this.closed) return;
    this.closed = true;
    this.failure = error;
    for (const item of this.pending.values()) { clearTimeout(item.timer); item.reject(error); }
    this.pending.clear();
    this.socket.destroy();
    for (const listener of this.listeners) { try { listener({ type: 'disconnected', error: error.message }); } catch {} }
  }
  close() { this.fail(new Error('Application connection closed')); }
}
