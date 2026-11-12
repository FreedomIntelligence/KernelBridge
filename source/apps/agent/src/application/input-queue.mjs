import { randomUUID } from 'node:crypto';

/** A client-side view only. The Executor Journal is the sole queue authority. */
export class InputQueue {
  constructor(client) {
    this.client = client; this.deliveries = []; this.pending = new Map(); this.consumed = new Set();
    this.uncertain = new Map();
  }
  async accept(text, kind = 'prompt', channel = 'tui') {
    const retained = kind !== 'control' && [...this.uncertain.values()].find(item => item.text === text);
    if (retained) {
      const current = (await this.client.state()).inputs?.[retained.input_id];
      if (!current) throw new Error(`Original input ${retained.input_id} is still unresolved; no replacement was submitted`);
      if (current.status === 'queued') this.enqueue(current);
      else this.complete(current);
      return current;
    }
    // Repeating a submit after an uncertain acknowledgement keeps the same identity.
    const key = JSON.stringify([text, kind, channel]);
    const id = this.pending.get(key) ?? randomUUID();
    this.pending.set(key, id);
    const receipt = await this.client.input(text, { inputId: id, kind, channel });
    this.pending.delete(key);
    if (kind !== 'control' && receipt.status === 'queued') this.enqueue(receipt);
    return receipt;
  }
  consume(text) {
    const receipt = this.take(text);
    this.complete(receipt);
    return receipt;
  }
  take(text) {
    const index = this.deliveries.findIndex(item => item.text === text);
    if (index < 0) throw new Error('Pi input has no durable host receipt');
    return this.deliveries.splice(index, 1)[0];
  }
  complete(receipt) {
    this.deliveries = this.deliveries.filter(item => item.input_id !== receipt.input_id);
    this.consumed.add(receipt.input_id);
    this.uncertain.delete(receipt.input_id);
  }
  retain(receipt) {
    this.deliveries = this.deliveries.filter(item => item.input_id !== receipt.input_id);
    this.uncertain.set(receipt.input_id, receipt);
  }
  enqueue(receipt) {
    if (this.consumed.has(receipt.input_id)) return false;
    if (!this.deliveries.some(item => item.input_id === receipt.input_id)) this.deliveries.push(receipt);
    return true;
  }
}
