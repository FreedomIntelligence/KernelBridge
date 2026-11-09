import { AsyncLocalStorage } from 'node:async_hooks';
import { join } from 'node:path';
import { canonicalKey } from '../../run/scoring/canonical.ts';
import { GatewayMcpTransport } from './mcp-transport.mjs';

const PREFIX = 'mcp__kernelcat__';
const WRAPPERS = new Set(['codemode', 'tool_search']);

function cleanResult(result) {
  if (!result || !Array.isArray(result.content) || result.content.some(block =>
    block.type !== 'text' || typeof block.text !== 'string')) {
    throw new Error('Native tool returned unsupported non-text content');
  }
  return { content: result.content.map(({ text }) => ({ type: 'text', text })),
    isError: result.isError === true,
    ...(result.structuredContent === undefined ? {} : { structuredContent: result.structuredContent }) };
}

function failedResult(error) {
  return { content: [{ type: 'text', text: String(error) }], isError: true };
}

/** Native discovery/composition retains one Host permit around its actual SDK executor. */
export function createNativeBridge(sdk, rpc, specifications, directory, sealedCall, fail, receipts, options = {}) {
  const scope = new AsyncLocalStorage();
  const leaves = specifications.filter(specification => !['finish', 'delegate'].includes(specification.name));
  const names = new Set(leaves.map(specification => specification.name));
  const builtins = options.tools ?? [];
  const builtinNames = new Set(builtins.map(tool => tool.name));

  async function invoke(name, arguments_) {
    const invocation = scope.getStore();
    if (!invocation || !names.has(name)) throw new Error('MCP call has no live native permit');
    const sequence = invocation.nextSequence++;
    if (sequence >= 64) throw new Error('Native invocation exceeds its 64-call bound');
    try {
      const response = await rpc.call('native_call', { permit: invocation.permit, sequence, name, arguments: arguments_ });
      const outcome = response.outcome;
      if (!outcome || outcome.status === 'unknown' || response.stop) {
        throw new Error('Native effect is unresolved; inspect its original receipt');
      }
      invocation.nestedReplies[sequence] = { action_id: response.action_id, outcome: { status: outcome.status } };
      return { content: [{ type: 'text', text: canonicalKey(response) }],
        structuredContent: response, isError: outcome.status !== 'completed' };
    } catch (error) {
      invocation.failure ??= error;
      fail(error);
      throw error;
    }
  }

  function wrap(factory) {
    return pi => factory(new Proxy(pi, {
      get(target, property, receiver) {
        if (property !== 'registerTool') return Reflect.get(target, property, receiver);
        return definition => pi.registerTool({ ...definition, execute: async (...args) => {
          const enclosing = scope.getStore();
          if (enclosing) {
            if (!definition.name.startsWith(PREFIX) || !names.has(definition.name.slice(PREFIX.length))) {
              throw new Error('Native scripts may call only the sealed Gateway MCP leaf catalogue');
            }
            return await definition.execute(...args);
          }
          try {
            return await execute(definition, args);
          } catch (error) {
            fail(error);
            throw error;
          }
        } });
      },
    }));
  }

  async function execute(definition, args) {
    const [id, arguments_] = args;
    if (!builtinNames.has(definition.name) && !WRAPPERS.has(definition.name) &&
        !(definition.name.startsWith(PREFIX) && names.has(definition.name.slice(PREFIX.length)))) {
      throw new Error(`Native tool has no permitted binding: ${definition.name}`);
    }
    const call = sealedCall(id, definition.name, arguments_);
    options.onStarted?.(id);
    const permission = await rpc.call('native_begin', call);
    if (permission.cached) {
      const result = permission.outcome.data.result ?? failedResult(permission.outcome.data.reason ?? 'Tool admission denied');
      receipts?.observeNative({ toolCallId: id, toolName: definition.name, result,
        response: { action_id: permission.action_id ?? call.id, outcome: permission.outcome } });
      options.onCompleted?.(id);
      return result;
    }
    if (typeof permission.permit !== 'string') throw new Error('Native tool has no Host permit');
    const invocation = { permit: permission.permit, nextSequence: 0, failure: null, nestedReplies: [] };
    let result;
    try {
      result = await scope.run(invocation, () => definition.execute(...args));
    } catch (error) {
      if (invocation.failure) throw invocation.failure;
      result = failedResult(error);
    }
    if (invocation.failure) throw invocation.failure;
    if (builtinNames.has(definition.name)) {
      result = { ...result, content: [...result.content,
        { type: 'text', text: `Execution receipt: ${permission.permit}` }] };
    }
    const clean = cleanResult(result);
    const response = await rpc.call('native_settle', { permit: invocation.permit,
      status: clean.isError ? 'failed' : 'completed', result: clean });
    receipts?.observeNative({ toolCallId: id, toolName: definition.name, result, response }, invocation.nestedReplies);
    options.onCompleted?.(id);
    return result;
  }

  const mcp = sdk.createMcpExtension({
    logPath: join(directory, 'mcp.log'),
    loadConfig: () => ({ errors: [], autoEnableCodemode: false, servers: [{
      name: 'kernelcat', source: 'KernelCat explicit finite Gateway binding', scope: 'extension',
      config: { type: 'stdio', command: 'kernelcat-in-process-gateway', exposure: 'codemode', timeout: options.timeout ?? 300 },
    }] }),
    createTransport(entry) {
      if (entry.name !== 'kernelcat') throw new Error('Unknown MCP server binding');
      return new GatewayMcpTransport(leaves, invoke);
    },
  });
  return {
    factories: [wrap(pi => { for (const tool of builtins) pi.registerTool(tool); }),
      ...(options.codemode === false ? [] : [wrap(sdk.createCodemodeExtension({ models: false })), wrap(sdk.createToolSearchExtension()), wrap(mcp)])],
    isNested: () => scope.getStore() !== undefined,
  };
}
