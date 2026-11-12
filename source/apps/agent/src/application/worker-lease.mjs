import { randomUUID } from 'node:crypto';
import { AsyncLocalStorage } from 'node:async_hooks';

export class WorkerLease {
  constructor(client, lease) { this.client = client; this.lease = lease; this.context = {}; this.modelGeneration = new AsyncLocalStorage(); }
  static async claim(client, ticket) {
    const result = await client.call('claim', { ticket });
    return new WorkerLease(client, result.lease);
  }
  async call(method, params = {}) {
    const generation = this.modelGeneration.getStore();
    const context = generation && ['model', 'child_model', 'application_compact'].includes(method)
      ? { ...this.context, model_generation_id: generation } : this.context;
    return this.client.call('effect', { method, params, call_id: randomUUID(), context }, 86400000);
  }
  withModelGeneration(generation, run) { return this.modelGeneration.run(generation, run); }
  bind(context) {
    if (context.input_id && context.input_id !== this.context.input_id) this.context = {};
    this.context = { ...this.context, ...context };
  }
}
