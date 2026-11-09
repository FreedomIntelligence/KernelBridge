import { MAX_LAUNCH_BYTES, digestBytes, loadLaunchManifest, readBoundedUtf8 } from "../tasks/launch-manifest.ts";
import type { WorkerConnectionOptions } from "../clients/worker-client.ts";
import { KernelCatSession } from "./session.ts";

export interface SessionSelection {
  readonly manifest: string;
  readonly applicationSessionId?: string;
}

/** Importing or constructing the client starts no process and performs no effect. */
export class KernelCat {
  private readonly worker: WorkerConnectionOptions;

  constructor(options: WorkerConnectionOptions) {
    this.worker = { ...options };
  }

  async createSession(selection: SessionSelection): Promise<KernelCatSession> {
    this.worker.signal?.throwIfAborted();
    await readBoundedUtf8(this.worker.config, "Worker configuration", MAX_LAUNCH_BYTES);
    const loaded = await loadLaunchManifest(selection.manifest);
    const identity = selection.applicationSessionId ?? `application-${digestBytes(JSON.stringify([loaded.manifest.account_id, loaded.manifest.run_id])).slice(7)}`;
    if (typeof identity !== "string" || identity.length === 0 || identity.length > 512) {
      throw new Error("Application session identity must contain between 1 and 512 characters");
    }
    return new KernelCatSession(this.worker, selection.manifest, loaded, identity);
  }

  selectSession(selection: SessionSelection): Promise<KernelCatSession> {
    return this.createSession(selection);
  }
}

export { KernelCatSession, SessionLifecycleError, UnsupportedCapabilityError } from "./session.ts";
export type { SessionStatus, SessionResult, StreamedRun } from "./session.ts";
export type { SessionEvent, SessionListener } from "./events.ts";
export type { RunOptions } from "./requests.ts";
export type { WorkerConnectionOptions } from "../clients/worker-client.ts";
export type { GoalPursuitBinding, ManagedInputRequest, ManagedInputReceipt, PrepareTrialRequest, ReplaceSessionRequest, RuntimeState } from "../generated/runtime.ts";
export type { ExecutionStamp } from "../generated/runtime-control.ts";
export { LoopXGoal, LoopXFailure } from "./goal.ts";
export type { LoopXGoalOptions, GoalTurnRequest, GoalObservation, GoalContinuationResult, GoalManagedSession, GoalManagedRun, GoalNextStop } from "./goal.ts";

export { loadGoalPursuitBinding, pursueGoal } from "./pursuit.ts";
export type { GoalPursuitOptions, GoalPursuitResult } from "./pursuit.ts";
export { createStageRouterNext } from "./next-trial.ts";
export type { StageRouterNextOptions, StageRouterNext } from "./next-trial.ts";
