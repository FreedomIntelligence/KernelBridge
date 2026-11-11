import type { EffectRpc } from "../clients/rpc.ts";
import { wireValidator } from "../clients/wire-schemas.ts";
import { compileContext } from "../context/compiler.ts";
import type { ContextSource } from "../generated/context.ts";
export type ModelInputProjection = (harness: HarnessInput, source: ContextSource) => HarnessInput;
import { validatedSnapshot } from "../context/json-snapshot.ts";
import { SCHEMA } from "../generated/harness.ts";
import type { ChildModelTurnRequest, HarnessInput, ModelTurnRequest } from "../generated/harness.ts";
import { canonicalKey } from "../run/scoring/canonical.ts";

const validateTurn = wireValidator<ModelTurnRequest>(SCHEMA.$id, "ModelTurnRequest");
const validateChild = wireValidator<ChildModelTurnRequest>(SCHEMA.$id, "ChildModelTurnRequest");
const validateHarness = wireValidator<HarnessInput>(SCHEMA.$id, "HarnessInput");

function admittedRoutes(value: unknown): readonly string[] {
  if (!Array.isArray(value) || value.length === 0 ||
      value.some((route) => typeof route !== "string" || route.length === 0)) {
    throw new Error("Model preparation has no admitted route order");
  }
  return [...value];
}

async function modelTurn(rpc: EffectRpc, request: ModelTurnRequest, native: boolean, project?: ModelInputProjection): Promise<Record<string, unknown>> {
  if (native !== (request.harness !== undefined)) throw new Error("Pi input differs from the admitted Harness capability");
  const prepared = await rpc.call("prepare_turn");
  if (prepared.stop === true) return prepared;
  let source = prepared.context_source;
  if (native && source === undefined) throw new Error("Native model preparation lacks its protected context source");
  for (const route of admittedRoutes(prepared.routes)) {
    const context = source === undefined ? undefined : compileContext(source, { harnessConversation: native }).context;
    const params: Record<string, unknown> = { route };
    if (context !== undefined) params.context = context;
    if (request.harness !== undefined) {
      const composed = validatedSnapshot({ ...request.harness,
        system_prompt: `KERNELCAT_CONTEXT_V1\n${canonicalKey(context)}\nEND_KERNELCAT_CONTEXT_V1\n${request.harness.system_prompt}`,
      }, validateHarness, "Owner-composed Pi input");
      params.harness = project && context ? project(composed, context) : composed;
    }
    // Only an acknowledged failed attempt advances the admitted order. Throws
    // (including lost acknowledgements) escape without another model request.
    const reply = await rpc.call("model", params);
    if (reply.stop === true || reply.failed !== true) return reply;
    if (source !== undefined && reply.context_source === undefined) {
      throw new Error("Failed model attempt lacks the refreshed budget context");
    }
    source = reply.context_source;
  }
  throw new Error("Admitted model routes exhausted without an owner stop");
}

/** The Pi process supplies a transcript or permit, never a route or budget. */
export function controlledModels(rpc: EffectRpc, project?: ModelInputProjection): EffectRpc {
  let native: boolean | undefined;
  // Concurrent delegations each hold their own permit and admitted route order.
  type Child = { readonly permit: string; readonly routes: readonly string[];
    settlement?: { readonly request: string; readonly result: Promise<Record<string, unknown>> } };
  const children = new Map<string, Child>();

  async function stoppedChild(child: Child): Promise<Record<string, unknown>> {
    // Only an acknowledged settlement closes a permit. An uncertain reply
    // still rejects through the original promise and fences the transport.
    const result = await child.settlement!.result;
    const outcome = result.outcome as { status: string; data?: { reason?: string } };
    return { failed: true, stop: true, reason: outcome.data?.reason ?? "child_closed", receipt: child.permit };
  }

  async function childTurn(params: Readonly<Record<string, unknown>>): Promise<Record<string, unknown>> {
    const request = validatedSnapshot(params, validateChild, "Child model turn");
    const child = children.get(request.permit);
    if (!child) throw new Error("Child model turn has no live owner permit");
    for (const route of child.routes) {
      if (child.settlement) return stoppedChild(child);
      const reply = await rpc.call("child_model", { permit: child.permit, route });
      if (reply.stop === true || reply.failed !== true) return reply;
    }
    throw new Error("Admitted child routes exhausted without an owner stop");
  }

  return {
    async call(method, params = {}) {
      if (["prepare_turn", "model", "child_model"].includes(method)) {
        throw new Error(`Pi cannot invoke protected model decision: ${method}`);
      }
      if (method === "hello") {
        if (native !== undefined) throw new Error("Pi model capability is already bound");
        const hello = await rpc.call(method, params);
        native = hello.native_harness !== undefined;
        return hello;
      }
      if (method === "model_turn") {
        if (native === undefined) throw new Error("Model turn precedes the owner handshake");
        return await modelTurn(rpc, validatedSnapshot(params, validateTurn, "Model turn"), native, project);
      }
      if (method === "child_model_turn") return await childTurn(params);
      if (method === "child_tool" && typeof params.permit === "string") {
        const child = children.get(params.permit);
        if (child?.settlement) return stoppedChild(child);
      }
      if (method === "plugin_begin") {
        const permission = await rpc.call(method, params);
        if (permission.cached === true) return permission;
        if (typeof permission.permit !== "string") throw new Error("Delegation has no owner permit");
        if (children.has(permission.permit)) throw new Error("Delegation permit is already live");
        children.set(permission.permit, { permit: permission.permit, routes: admittedRoutes(permission.routes) });
        const { routes: _routes, ...execution } = permission;
        return execution;
      }
      if (method === "plugin_settle" && typeof params.permit === "string") {
        const child = children.get(params.permit);
        if (child) {
          const request = canonicalKey(params);
          if (child.settlement) {
            if (child.settlement.request !== request) throw new Error("Child settlement differs from its original request");
            return child.settlement.result;
          }
          const result = Promise.resolve().then(async () => {
            const reply = await rpc.call(method, params);
            const outcome = reply.outcome as { status?: unknown } | undefined;
            if (!outcome || !["completed", "failed"].includes(String(outcome.status))) {
              throw new Error("Child settlement is unresolved; inspect its original receipt");
            }
            return reply;
          });
          // Keep the original result, including rejection, for late Pi work.
          // Deleting here turns a known cancellation into a transport failure
          // and can prevent another child's admitted effect from settling.
          child.settlement = { request, result };
          return result;
        }
      }
      const result = await rpc.call(method, params);
      return result;
    },
  };
}
