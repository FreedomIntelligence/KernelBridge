import { projectHarness } from '../harness/pi/input.ts';
import { canonicalKey } from './scoring/canonical.ts';

/** Pi executes owner-admitted turns and sealed tools; LoopX owns model decisions. */
export class RunController {
  constructor(rpc, native, delegate, isNested, receipts, { persistent = false } = {}) {
    this.rpc = rpc;
    this.native = native;
    this.delegate = delegate;
    this.isNested = isNested;
    this.receipts = receipts;
    this.persistent = persistent;
    this.active = null;
    this.stopped = false;
    this.failure = null;
    this.batchResult = null;
    this.started = new Set();
    this.completed = new Set();
  }

  fail(error) { this.failure ??= error instanceof Error ? error : new Error(String(error)); }

  async model(sdkContext) {
    const params = this.native ? { harness: projectHarness(sdkContext) } : {};
    const reply = await this.rpc.call('model_turn', params);
    if (reply.stop) {
      this.stopped = true;
      this.stopResult = reply.result;
      this.active = null;
      return { text: 'Invocation stopped by host.', calls: [] };
    }
    this.active = reply;
    this.started.clear();
    this.completed.clear();
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

  validationExtension() {
    if (!this.native) return () => {};
    return pi => {
      pi.on('tool_call', event => {
        if (event.parentToolCallId || this.isNested()) return;
        if (this.active?.calls.length > 1 && this.active.calls.some(call => call.name === 'finish')) {
          return { block: true, reason: 'finish must be the only tool call in its turn' };
        }
        // Pi may coerce arguments. Record a known, unexecuted error instead of
        // dispatching bytes different from the original model receipt.
        try { this.sealedCall(event.toolCallId, event.toolName, event.input); }
        catch (error) { return { block: true, reason: String(error) }; }
      });
      pi.on('tool_execution_end', async event => {
        if (!this.active || this.stopped || this.completed.has(event.toolCallId)
            || this.started.has(event.toolCallId) || event.parentToolCallId) return;
        const call = this.active.calls.find(item => item.id === event.toolCallId);
        if (!call) return;
        try {
          const result = event.result;
          const reason = (result?.content ?? []).filter(block => block.type === 'text')
            .map(block => block.text).join('\n').slice(0, 8192);
          const response = await this.rpc.call('tool_error', { call, reason: reason || 'Pi rejected this tool before execution' });
          this.receipts?.observeDirect({ toolCallId: call.id, toolName: call.name,
            result: { content: result.content, isError: true }, response });
          this.completed.add(call.id);
        } catch (error) { this.fail(error); throw error; }
      });
    };
  }

  async tool(id, name, arguments_, signal) {
    if (this.stopped) return this.stoppedResult();
    if (this.isNested()) throw new Error('Native scripts must use the Gateway MCP leaf binding');
    try {
      const call = this.sealedCall(id, name, arguments_);
      this.started.add(id);
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
      const result = { content: [{ type: 'text', text: canonicalKey(response) }],
        details: response, isError: response.outcome?.status !== 'completed' };
      // Delegation's public settlement lacks the leaf receipt identity; retain
      // that output without fabricating cleanup provenance from wrapper success.
      if (name !== 'delegate') this.receipts?.observeDirect({ toolCallId: id, toolName: name, result, response });
      this.completed.add(id);
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
      this.stopResult = ended.result;
      return { action: 'end' };
    }
    // A final model answer already has a durable end_turn checkpoint. Let Pi
    // settle (or deliver its queued user input) without requesting a synthetic
    // assistant turn merely to rediscover that the invocation has finished.
    if (this.persistent && !message.content.some(item => item.type === 'toolCall')) return { action: 'end' };
    return { action: 'continue' };
  }
}
