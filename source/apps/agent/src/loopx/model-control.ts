import type { EffectRpc } from "../clients/rpc.ts";
import { wireValidator } from "../clients/wire-schemas.ts";
import { compileContext } from "../context/compiler.ts";
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

async function modelTurn(rpc: EffectRpc, request: ModelTurnRequest, native: boolean): Promise<Record<string, unknown>> {
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
      params.harness = validatedSnapshot({ ...request.harness,
        system_prompt: `KERNELCAT_CONTEXT_V1\n${canonicalKey(context)}\nEND_KERNELCAT_CONTEXT_V1\n${request.harness.system_prompt}`,
      }, validateHarness, "Owner-composed Pi input");
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
export function controlledModels(rpc: EffectRpc): EffectRpc {
  let native: boolean | undefined;
  let child: { readonly permit: string; readonly routes: readonly string[] } | undefined;

  async function childTurn(params: Readonly<Record<string, unknown>>): Promise<Record<string, unknown>> {
    const request = validatedSnapshot(params, validateChild, "Child model turn");
    if (!child || request.permit !== child.permit) throw new Error("Child model turn has no live owner permit");
    for (const route of child.routes) {
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
        return await modelTurn(rpc, validatedSnapshot(params, validateTurn, "Model turn"), native);
      }
      if (method === "child_model_turn") return await childTurn(params);
      if (method === "plugin_begin") {
        if (child) throw new Error("Previous child permit is not settled");
        const permission = await rpc.call(method, params);
        if (permission.cached === true) return permission;
        if (typeof permission.permit !== "string") throw new Error("Delegation has no owner permit");
        child = { permit: permission.permit, routes: admittedRoutes(permission.routes) };
        const { routes: _routes, ...execution } = permission;
        return execution;
      }
      const result = await rpc.call(method, params);
      if (method === "plugin_settle" && params.permit === child?.permit) child = undefined;
      return result;
    },
  };
}
