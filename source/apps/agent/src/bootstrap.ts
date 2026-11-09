import { constants } from "node:fs";
import { open } from "node:fs/promises";
import { isAbsolute } from "node:path";
import { runAdministration, runRuntimeWithWorker, runWithWorker } from "./clients/worker-client.ts";
import type { ProductOptions } from "./cli/arguments.ts";
import { knowledgeConsumerStatus, knowledgeFenceReceipt, knowledgeFenceRequest, runtimeValue } from "./clients/runtime.ts";
import {
  MAX_LAUNCH_BYTES, MAX_STOP_REQUEST_BYTES, readBoundedUtf8,
} from "./tasks/launch-manifest.ts";
import { KernelCat } from "./sdk/index.ts";
import { pursueGoal } from "./sdk/pursuit.ts";
import type { EffectRpc } from "./clients/rpc.ts";
import type { RsiCollectionResult, RsiReplayResult } from "./generated/rsi.ts";
import { readRsi } from "./rsi/records.ts";

const MAX_KNOWLEDGE_FENCE_BYTES = 16 * 1024 * 1024;

async function finishBootstrap(rpc: EffectRpc, result: Record<string, unknown>): Promise<Record<string, unknown>> {
  const completion = await rpc.call("complete");
  if (completion.status !== "bootstrap_complete") throw new Error("Worker did not confirm bootstrap protocol completion");
  return { status: "finished", ...result, bootstrap: completion };
}

async function loadFenceRequest(path: string): Promise<unknown> {
  if (!isAbsolute(path)) throw new Error("Knowledge fence request must be an absolute path");
  // Inspect the opened descriptor: neither a symlink swap nor an unwritten FIFO
  // may bypass the administrative regular-file boundary or block startup.
  const handle = await open(path, constants.O_RDONLY | constants.O_NONBLOCK | constants.O_NOFOLLOW);
  try {
    if (!(await handle.stat()).isFile()) throw new Error("Knowledge fence request must be a regular file");
    const bytes = Buffer.alloc(MAX_KNOWLEDGE_FENCE_BYTES + 1);
    let size = 0;
    while (size < bytes.length) {
      const read = await handle.read(bytes, size, bytes.length - size, null);
      if (read.bytesRead === 0) break;
      size += read.bytesRead;
    }
    if (size > MAX_KNOWLEDGE_FENCE_BYTES) throw new Error("Knowledge fence request exceeds its byte limit");
    return JSON.parse(new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(bytes.subarray(0, size)));
  } finally {
    await handle.close();
  }
}

/** The product owns one finite Worker; importing this module starts no process. */
export async function runProduct(options: ProductOptions): Promise<Record<string, unknown>> {
  if (options.command === "pursue") return await pursueGoal(options.goal);
  if (options.command === "rsi-replay") {
    await readBoundedUtf8(options.config, "Worker configuration", MAX_LAUNCH_BYTES);
    await readBoundedUtf8(options.task, "Worker task", MAX_LAUNCH_BYTES);
    return { ...readRsi<RsiReplayResult>(await runAdministration(options,
      ["rsi-replay", "--config", options.config, "--task", options.task]), "RsiReplayResult") };
  }
  if (options.command === "loopx-cycle") {
    await readBoundedUtf8(options.plan, "LoopX plan", MAX_LAUNCH_BYTES);
    return await runAdministration(options, ["loopx-cycle", "--plan", options.plan,
      ...(options.execute ? ["--execute"] : [])]);
  }
  if (options.command === "rsi-collect") {
    await readBoundedUtf8(options.config, "Worker configuration", MAX_LAUNCH_BYTES);
    await readBoundedUtf8(options.task, "Worker task", MAX_LAUNCH_BYTES);
    const response = await runAdministration(options, ["rsi-collect", "--config", options.config,
      "--task", options.task, "--action-id", options.actionId]);
    const result = readRsi<RsiCollectionResult>(response, "RsiCollectionResult");
    if (result.action_id !== options.actionId || result.settled !== (result.status !== "unknown")) {
      throw new Error("RSI collection result differs from the selected original action");
    }
    return { ...result };
  }
  if (options.command === "run" && options.mode === "legacy") {
    const { withPreparedPi } = await import("./loopx/pi-host.ts");
    const result = await withPreparedPi(run => runWithWorker(options, run));
    if (result.status !== "paused") return result;
    // This finite postcondition runs only after Pi and its original Worker exit.
    // The request remains in the original Journal; it never starts another turn.
    const restored = readRsi<RsiReplayResult>(await runAdministration(options,
      ["rsi-replay", "--config", options.config, "--task", options.task]), "RsiReplayResult");
    return restored.status === "restored" ? { ...result, replay: restored } : result;
  }
  // Raw config stays YAML owned by Worker. Bound and decode before launch; Worker
  // validates protected paths and configuration capabilities before opening state.
  await readBoundedUtf8(options.config, "Worker configuration", MAX_LAUNCH_BYTES);
  if (options.command === "run") {
    const session = await new KernelCat(options).createSession({ manifest: options.manifest });
    try {
      return await session.run({ resume: options.resume, nextTrial: options.nextTrial });
    } finally {
      await session.dispose();
    }
  }
  if (options.command === "account-create") {
    const request = runtimeValue("AccountCreateRequest", {
      schema_version: "kernelcat.runtime-account-create/v1", intent_id: options.intentId, credits: options.credits,
      ...(options.knowledgeAuthorityId === undefined ? {} : { knowledge_authority_id: options.knowledgeAuthorityId }),
      ...(options.multiTrial === undefined ? {} : { multi_trial: options.multiTrial }),
    });
    return await runRuntimeWithWorker(options, async (rpc) => await finishBootstrap(rpc, {
      account: runtimeValue("AccountReceipt", await rpc.call("account_create", { ...request })) }));
  }
  if (options.command === "runtime-state") {
    const request = runtimeValue("RuntimeStateRequest", { run_id: options.runId, trial_id: options.trialId });
    return await runRuntimeWithWorker(options, async (rpc) => await finishBootstrap(rpc, {
      runtime: runtimeValue("RuntimeState", await rpc.call("runtime_state", { ...request })) }));
  }
  if (options.command === "knowledge-sync") {
    const request = runtimeValue("KnowledgeSyncRequest", { run_id: options.runId, trial_id: options.trialId,
      ...(options.limit === undefined ? {} : { limit: options.limit }) });
    return await runRuntimeWithWorker(options, async (rpc) => {
      const status = knowledgeConsumerStatus(await rpc.call("knowledge_sync", { ...request }));
      if (status.registration.consumer.run_id !== request.run_id || status.registration.consumer.trial_id !== request.trial_id) {
        throw new Error("Knowledge status differs from the selected original Run/Trial consumer");
      }
      return await finishBootstrap(rpc, { knowledge: { ...status } });
    });
  }
  if (options.command === "knowledge-fence") {
    const request = knowledgeFenceRequest(await loadFenceRequest(options.request));
    return await runRuntimeWithWorker(options, async (rpc) => await finishBootstrap(rpc, {
      fence: { ...knowledgeFenceReceipt(await rpc.call("knowledge_fence", { ...request }), request) } }));
  }
  const request = runtimeValue("StopRunRequest",
    JSON.parse(await readBoundedUtf8(options.request, "Stop request", MAX_STOP_REQUEST_BYTES)));
  return await runRuntimeWithWorker(options, async (rpc) => await finishBootstrap(rpc, {
    runtime: runtimeValue("RuntimeState", await rpc.call("stop_run", { ...request })) }));
}
