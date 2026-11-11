import { MAX_LAUNCH_BYTES, digestBytes, loadLaunchManifest, readBoundedUtf8 } from "../tasks/launch-manifest.ts";
import type { WorkerConnectionOptions } from "../clients/worker-client.ts";
import { runWithWorker } from "../clients/worker-client.ts";
import { withPreparedPi } from "../loopx/pi-host.ts";
import type { HarnessOptions } from "../harness/types.ts";
import { KernelCatSession } from "./session.ts";
import { ApplicationSession } from "./application-session.mjs";
import type { ApplicationOptions } from "./application-session.mjs";

export interface SessionSelection {
  readonly manifest: string;
  readonly applicationSessionId?: string;
}

/** task.goal is the initial Pi prompt; task.contract and seed_files supply context. */
export interface TaskRunOptions extends HarnessOptions {
  readonly task: string;
}

/** Importing or constructing the client starts no process and performs no effect. */
export class KernelCat {
  private readonly worker: WorkerConnectionOptions;

  constructor(options: WorkerConnectionOptions) {
    this.worker = { ...options };
  }

  /** One bounded Pi session with the same tools, Gateway and receipts as the CLI.
   * Goal mode uses createSession + LoopXGoal; it does not add another agent loop.
   * A failed invocation is never automatically retried.
   */
  async runTask(options: TaskRunOptions): Promise<Record<string, unknown>> {
    const signals = [this.worker.signal, options.signal].filter((value): value is AbortSignal => value !== undefined);
    const signal = signals.length ? AbortSignal.any(signals) : undefined;
    signal?.throwIfAborted();
    await readBoundedUtf8(this.worker.config, "Worker configuration", MAX_LAUNCH_BYTES);
    await readBoundedUtf8(options.task, "Worker task", MAX_LAUNCH_BYTES);
    return await withPreparedPi(run => runWithWorker({ ...this.worker, task: options.task, signal },
      rpc => run(rpc, { signal, onEvent: options.onEvent })), { signal });
  }

  createSession(selection: SessionSelection): Promise<KernelCatSession>;
  createSession(selection: Omit<ApplicationOptions, "config" | "python" | "expectedPythonModules">): Promise<ApplicationSession>;
  async createSession(selection: SessionSelection | Omit<ApplicationOptions, "config" | "python" | "expectedPythonModules">): Promise<KernelCatSession | ApplicationSession> {
    if (!("manifest" in selection)) return this.createApplicationSession(selection);
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

  createApplicationSession(options: Omit<ApplicationOptions, "config" | "python" | "expectedPythonModules">): Promise<ApplicationSession> {
    return ApplicationSession.create({ ...options, config: this.worker.config, python: this.worker.python,
      expectedPythonModules: this.worker.expectedPythonModules });
  }

  attachApplicationSession(identity: string): Promise<ApplicationSession> {
    return ApplicationSession.attach(identity);
  }
}

export { ApplicationSession } from "./application-session.mjs";
export type { ApplicationOptions, ApplicationInputOptions } from "./application-session.mjs";

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
