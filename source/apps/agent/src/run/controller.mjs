import { projectHarness } from '../harness/pi/input.ts';
import { canonicalKey } from './scoring/canonical.ts';

/** Pi executes owner-admitted turns and sealed tools; LoopX owns model decisions. */
export class RunController {
  constructor(rpc, native, delegate, isNested, receipts) {
    this.rpc = rpc;
    this.native = native;
    this.delegate = delegate;
    this.isNested = isNested;
    this.receipts = receipts;
    this.active = null;
    this.stopped = false;
    this.failure = null;
    this.batchResult = null;
  }

  fail(error) { this.failure ??= error instanceof Error ? error : new Error(String(error)); }

  async model(sdkContext) {
    const params = this.native ? { harness: projectHarness(sdkContext) } : {};
    const reply = await this.rpc.call('model_turn', params);
    if (reply.stop) {
      this.stopped = true;
      this.active = null;
      return { text: 'Invocation stopped by host.', calls: [] };
    }
    this.active = reply;
    this.batchResult = null;
    return reply;
  }

  sealedCall(id, name, arguments_) {
    if (this.stopped || this.failure || !this.active) throw new Error('Tool has no admitted model turn');
    const call = this.active.calls.find(item => item.id === id);
    if (!call || call.name !== name || canonicalKey(call.arguments) !== canonicalKey(arguments_)) {
      throw new Error('Tool differs from the Host-sealed model call');
    }
    return call;
  }

  async tool(id, name, arguments_, signal) {
    if (this.stopped) return this.stoppedResult();
    if (this.isNested()) throw new Error('Native scripts must use the Gateway MCP leaf binding');
    try {
      const call = this.sealedCall(id, name, arguments_);
      let response;
      if (this.active.parallel) {
        this.batchResult ??= this.rpc.call('batch', { proposal_id: this.active.proposal_id });
        const batch = await this.batchResult;
        response = batch.stop ? batch : batch.results[this.active.calls.findIndex(item => item.id === id)];
      } else {
        response = name === 'delegate' ? await this.delegate(call, signal) : await this.rpc.call('tool', call);
      }
      if (response.stop) {
        this.stopped = true;
        this.active = null;
        return this.stoppedResult();
      }
      const result = { content: [{ type: 'text', text: canonicalKey(response.outcome) }],
        details: response, isError: response.outcome?.status !== 'completed' };
      // Delegation's public settlement lacks the leaf receipt identity; retain
      // that output without fabricating cleanup provenance from wrapper success.
      if (name !== 'delegate') this.receipts?.observeDirect({ toolCallId: id, toolName: name, result, response });
      return result;
    } catch (error) {
      this.fail(error);
      throw error;
    }
  }

  stoppedResult() {
    return { content: [{ type: 'text', text: 'Host stopped this invocation.' }],
      details: { stopped: true }, terminate: true };
  }

  async finish(message, nativeCheckpoint) {
    if (this.failure || this.stopped || !this.active || ['error', 'aborted'].includes(message.stopReason)) {
      return { action: 'end' };
    }
    const ended = await this.rpc.call('end_turn', { proposal_id: this.active.proposal_id,
      ...(nativeCheckpoint === undefined ? {} : { native_checkpoint: nativeCheckpoint }) });
    this.active = null;
    if (ended.stop) {
      this.stopped = true;
      return { action: 'end' };
    }
    return { action: 'continue' };
  }
}
