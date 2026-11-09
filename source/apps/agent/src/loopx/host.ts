import type { EffectRpc } from "../clients/rpc.ts";
import type { HarnessRunner } from "../harness/types.ts";
import { SCHEMA } from "../generated/harness.ts";
import type { LoopXHostOpenResult } from "../generated/harness.ts";
import { validatedSnapshot } from "../context/json-snapshot.ts";
import { wireValidator } from "../clients/wire-schemas.ts";
import { runGovernedPi } from "./pi-host.ts";
import { applyIntervention } from "./intervention-control.ts";

const validateOpen = wireValidator<LoopXHostOpenResult>(SCHEMA.$id, "LoopXHostOpenResult");

/** Released LoopX envelopes remain opaque until its pinned authority verifies them. */
export async function executeLoopXHost(
  rpc: EffectRpc, request: string, run: HarnessRunner = runGovernedPi,
): Promise<Record<string, unknown>> {
  const opened = validatedSnapshot(await rpc.call("loopx_open", { request_json: request }),
    validateOpen, "LoopX host admission");
  if (opened.run_pi) {
    await run({
      async call(method, params = {}) {
        if (method.startsWith("loopx_")) throw new Error("Pi cannot invoke LoopX lifecycle controls");
        return await rpc.call(method, params);
      },
    });
  }
  if (opened.rsi_enabled) await applyIntervention(rpc);
  // runGovernedPi resolves only after Pi has exited. Finalization uses original
  // Worker completion; neither Pi nor this host supplies a replacement result.
  return await rpc.call("loopx_finish");
}
