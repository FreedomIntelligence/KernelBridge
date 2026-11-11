import { randomUUID } from 'node:crypto';

export class WorkerLease {
  constructor(client, lease) { this.client = client; this.lease = lease; this.context = {}; }
  static async claim(client, ticket) {
    const result = await client.call('claim', { ticket });
    return new WorkerLease(client, result.lease);
  }
  async call(method, params = {}) {
    return this.client.call('effect', { method, params, call_id: randomUUID(), context: this.context }, 86400000);
  }
  bind(context) {
    if (context.input_id && context.input_id !== this.context.input_id) this.context = {};
    this.context = { ...this.context, ...context };
  }
}
