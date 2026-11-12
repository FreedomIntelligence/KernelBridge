import {
  DEFAULT_MAX_PAYLOAD_BYTES, checkUploadWrite, decodedPayload,
  effectUpload, payloadPage, payloadPageBytes, payloadReference, supportsUpload, uploadReference,
  validatePayloadLimit, validatedResult,
} from './rpc-payload.ts';
import { DEFAULT_FRAME_BYTES, frames, validateFrameLimit } from './frames.mjs';
export const PROTOCOL = 'kernelcat.pi-worker/v3';
export { DEFAULT_FRAME_BYTES, frames };
export class RpcClient {
  constructor(input, output, limit = DEFAULT_FRAME_BYTES, maxPayloadBytes = DEFAULT_MAX_PAYLOAD_BYTES) {
    validateFrameLimit(limit);
    validatePayloadLimit(maxPayloadBytes);
    this.messages = frames(input, limit)[Symbol.asyncIterator]();
    this.output = output;
    this.limit = limit;
    this.nextId = 1;
    this.busy = false;
    this.failed = false;
    this.maxPayloadBytes = maxPayloadBytes;
  }

  async call(method, params = {}) {
    if (this.busy || this.failed) {
      throw new Error('RPC is busy or failed; no automatic retry');
    }
    this.busy = true;
    try {
      const data = JSON.stringify({ protocol: PROTOCOL, id: this.nextId, method, params });
      let result;
      if (Buffer.byteLength(data) > this.limit) {
        if (!supportsUpload(method)) {
          throw new Error('Request exceeds frame limit');
        }
        result = await this.#uploadEffect(method, params);
      } else {
        result = await this.#exchange(method, params);
      }
      return await this.#resolvePayload(result);
    } catch (error) {
      this.failed = true;
      throw error;
    } finally {
      this.busy = false;
    }
  }

  async #exchange(method, params) {
    const id = this.nextId++;
    const data = JSON.stringify({ protocol: PROTOCOL, id, method, params });
    if (Buffer.byteLength(data) > this.limit) {
      throw new Error('Request exceeds frame limit');
    }
    await new Promise((resolve, reject) => {
      this.output.write(data + '\n', error => {
        if (error) {
          reject(error);
        } else {
          resolve();
        }
      });
    });
    const next = await this.messages.next();
    if (next.done) {
      throw new Error('RPC lost connection');
    }
    const reply = next.value;
    if (!reply || typeof reply !== 'object' || Array.isArray(reply) ||
        Object.keys(reply).sort().join(',') !== 'id,protocol,result' ||
        reply.id !== id || reply.protocol !== PROTOCOL ||
        !reply.result || typeof reply.result !== 'object' || Array.isArray(reply.result)) {
      throw new Error('Invalid RPC response');
    }
    return validatedResult(reply.result);
  }

  async #resolvePayload(result) {
    if (!Object.hasOwn(result, 'transport')) {
      return result;
    }
    const { ref } = payloadReference(result, this.maxPayloadBytes);
    const bytes = Buffer.allocUnsafe(ref.bytes);
    let offset = 0;
    while (offset < ref.bytes) {
      const maximum = Math.min(payloadPageBytes(this.limit), ref.bytes - offset);
      const page = await this.#exchange('payload_read', { ref, offset, max_bytes: maximum });
      const data = payloadPage(page, ref, offset, maximum);
      data.copy(bytes, offset);
      offset += data.length;
    }
    return decodedPayload(bytes, ref);
  }

  async #uploadEffect(method, params) {
    const { bytes, begin } = effectUpload(method, params, this.maxPayloadBytes);
    // A begin result identifies an upload, not a downloadable response.
    const ref = uploadReference(await this.#exchange('payload_begin', begin), begin);
    const pageBytes = payloadPageBytes(this.limit);
    for (let offset = 0; offset < bytes.length; offset += pageBytes) {
      const data = bytes.subarray(offset, offset + pageBytes);
      const reply = await this.#exchange('payload_write', { ref, offset, data: data.toString('base64') });
      checkUploadWrite(reply, ref, offset + data.length);
    }
    return await this.#exchange('payload_call', { ref, method });
  }
}
// Pi owns scheduling; execution IDs and original tool calls come only from the host.
export function assistant(reply, model={api:'openai-completions',provider:'kernelcat',id:'controlled'}, timestamp=Date.now(), toolName='kernelcat_effect') {
  if(!reply || typeof reply.text!=='string' || !Array.isArray(reply.calls)) throw new Error('Invalid host turn');
  const content=[];
  if(reply.text) content.push({type:'text',text:reply.text});
  for(const call of reply.calls) {
    if(!call||typeof call.id!=='string'||typeof call.name!=='string'||!call.arguments||typeof call.arguments!=='object') throw new Error('Invalid host call');
    content.push({type:'toolCall',id:call.id,name:toolName ?? call.name,arguments:toolName === null ? call.arguments : call});
  }
  if(!content.length) content.push({type:'text',text:'Host stopped this invocation.'});
  const usage = reply.usage ?? {};
  const count = (...names) => {
    for (const name of names) if (Number.isSafeInteger(usage[name]) && usage[name] >= 0) return usage[name];
    return 0;
  };
  const cacheRead = count('cache_read_input_tokens', 'prompt_cache_hit_tokens');
  const cacheWrite = count('cache_creation_input_tokens');
  // OpenAI prompt_tokens includes cache hits; Anthropic input_tokens excludes them.
  const input = Object.hasOwn(usage, 'prompt_tokens') ? Math.max(0, count('prompt_tokens') - cacheRead) : count('input_tokens');
  const output = count('completion_tokens', 'output_tokens');
  return {role:'assistant',content,api:model.api,provider:model.provider,model:model.id,
    usage:{input,output,cacheRead,cacheWrite,totalTokens:Object.hasOwn(usage,'total_tokens') ? count('total_tokens') : input+output+cacheRead+cacheWrite,
      cost:{input:0,output:0,cacheRead:0,cacheWrite:0,total:0}},
    stopReason:reply.calls.length?'toolUse':'stop',timestamp};
}
