import type { EffectRpc } from "../clients/rpc.ts";
import { createRsiController } from "../rsi/controller.ts";
import { reduceExperiment } from "../rsi/reduction.ts";
import { selectKnowledge } from "../rsi/selection.ts";

/** Protected host decisions. Pi can request preparation/settlement, never a verdict. */
export function controlledRsi(rpc: EffectRpc, applicationBaseline?: () => Promise<{ baseline_record_id: string; record_sequence: number } | undefined>): EffectRpc {
  const controller = createRsiController(rpc, applicationBaseline);
  let enabled = false;
  return {
    async call(method, params = {}) {
      if (method === "hello") {
        const hello = await rpc.call(method, params);
        enabled = hello.rsi !== undefined;
        return hello;
      }
      if (method === "complete" && enabled) await controller.call("rsi_checkpoint");
      if (!method.startsWith("rsi_")) return await rpc.call(method, params);
      if (!enabled) throw new Error("Pi requested RSI without an admitted capability");
      if (Object.keys(params).length !== 0) throw new Error("Pi RSI lifecycle requests take no decisions or parameters");
      if (method === "rsi_prepare") {
        const candidates = await controller.call("rsi_prepare");
        if (candidates.stop === true) return { stop: true };
        await controller.call("rsi_select", { ...selectKnowledge(candidates) });
        return { stop: false };
      }
      if (method === "rsi_settle") {
        const response = await controller.call("rsi_pending");
        if (Object.keys(response).join(",") !== "pending") throw new Error("Invalid RSI pending response");
        const result = response.pending === null ? null : await reduceExperiment(response.pending, controller);
        return { result };
      }
      if (method === "rsi_checkpoint") return await controller.call(method);
      throw new Error(`Pi cannot invoke protected RSI decision: ${method}`);
    },
  };
}
