import { randomUUID } from 'node:crypto';

/** A client-side view only. The Executor Journal is the sole queue authority. */
export class InputQueue {
  constructor(client) { this.client = client; this.deliveries = []; this.pending = new Map(); this.consumed = new Set(); }
  async accept(text, kind = 'prompt', channel = 'tui') {
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
    const index = this.deliveries.findIndex(item => item.text === text);
    if (index < 0) throw new Error('Pi input has no durable host receipt');
    const receipt = this.deliveries.splice(index, 1)[0];
    this.consumed.add(receipt.input_id);
    return receipt;
  }
  enqueue(receipt) {
    if (!this.consumed.has(receipt.input_id) && !this.deliveries.some(item => item.input_id === receipt.input_id)) this.deliveries.push(receipt);
  }
}
