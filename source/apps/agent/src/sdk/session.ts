import { randomUUID } from "node:crypto";
import { readRuntime, runtimeValue } from "../clients/runtime.ts";
import type { EffectRpc } from "../clients/rpc.ts";
import { runRuntimeWithWorker } from "../clients/worker-client.ts";
import type { WorkerConnectionOptions } from "../clients/worker-client.ts";
import type { ManagedInputReceipt, ManagedInputRequest, RuntimeState } from "../generated/runtime.ts";
import { runGovernedPi } from "../loopx/pi-host.ts";
import { runManaged } from "../run/managed.ts";
import type { LoadedManifest } from "../tasks/launch-manifest.ts";
import { SessionEventStream } from "./events.ts";
import type { SessionEvent, SessionListener } from "./events.ts";
import { runRequests } from "./requests.ts";
import type { RunOptions } from "./requests.ts";

export type SessionStatus = "idle" | "running" | "interrupted" | "failed" | "disposed";
export interface SessionResult extends Record<string, unknown> {
  readonly status: string;
  readonly runtime_state: RuntimeState;
}
export interface StreamedRun {
  readonly events: AsyncIterable<SessionEvent>;
  readonly result: Promise<SessionResult>;
}
export class SessionLifecycleError extends Error {}
export class UnsupportedCapabilityError extends Error {}

/** A local application handle over the existing Run/Trial authority and finite Worker. */
export class KernelCatSession {
  readonly applicationSessionId: string;
  readonly capabilities = Object.freeze({ events: true, tokenStreaming: false, nativeFork: false, persistentNativeSession: false });
  private phase: SessionStatus = "idle";
  private readonly listeners = new Set<SessionListener>();
  private readonly reads = new Map<AbortController, Promise<Record<string, unknown>>>();
  private active: Promise<SessionResult> | undefined;
  private cancellation: AbortController | undefined;
  private connection: EffectRpc | undefined;
  private observation: RuntimeState | undefined;
  private selectedTrial: string;
  private invocationId = "";
  private sequence = 0;
  private pendingInputs = 0;
  private acceptedInputFence = false;
  private readonly eventQueue: SessionEvent[] = [];
  private dispatching = false;
  private readonly worker: WorkerConnectionOptions;
  private readonly manifestPath: string;
  private readonly loaded: LoadedManifest;
  private disposal: Promise<void> | undefined;

  constructor(
    worker: WorkerConnectionOptions,
    manifestPath: string,
    loaded: LoadedManifest,
    applicationSessionId: string,
  ) {
    this.worker = { ...worker };
    this.manifestPath = manifestPath;
    this.loaded = structuredClone(loaded);
    this.applicationSessionId = applicationSessionId;
    this.selectedTrial = loaded.manifest.trial_id;
  }

  get status(): SessionStatus { return this.phase; }
  get hasPendingInput(): boolean { return this.pendingInputs > 0 || this.acceptedInputFence; }
  get latestState(): RuntimeState | undefined { return this.observation === undefined ? undefined : structuredClone(this.observation); }

  subscribe(listener: SessionListener): () => void {
    this.assertOpen();
    this.listeners.add(listener);
    return () => { this.listeners.delete(listener); };
  }

  run(options: RunOptions = {}): Promise<SessionResult> {
    this.assertOpen();
    if (this.active !== undefined) throw new SessionLifecycleError("Session already has an active invocation");
    const cancellation = new AbortController();
    const signal = AbortSignal.any([cancellation.signal, ...[this.worker.signal, options.signal].filter((item): item is AbortSignal => item !== undefined)]);
    signal.throwIfAborted();
    this.cancellation = cancellation;
    // Only an explicit foreground invocation releases this conservative fence.
    // Input acknowledgement alone does not establish model consumption.
    this.acceptedInputFence = false;
    this.phase = "running";
    this.invocationId = randomUUID();
    this.sequence = 0;
    // Reserve the invocation before notifying observers, which may call back into this handle.
    const request: RunOptions = {
      ...options,
      resume: typeof options.resume === "object" ? structuredClone(options.resume) : options.resume,
      nextTrial: typeof options.nextTrial === "object" ? structuredClone(options.nextTrial) : options.nextTrial,
    };
    const operation = Promise.resolve().then(() => this.execute(request, signal));
    this.active = operation;
    this.emit("started", { run_id: this.loaded.manifest.run_id });
    void operation.then(() => this.release(operation), () => this.release(operation));
    return operation;
  }

  runStreamed(options: RunOptions = {}): StreamedRun {
    const stream = new SessionEventStream();
    const unsubscribe = this.subscribe(event => stream.push(event));
    try {
      const result = this.run(options);
      void result.then(() => { unsubscribe(); stream.close(); }, () => { unsubscribe(); stream.close(); });
      return { events: stream, result };
    } catch (error) {
      unsubscribe();
      stream.close();
      throw error;
    }
  }

  async input(value: ManagedInputRequest): Promise<ManagedInputReceipt> {
    this.assertOpen();
    const request = runtimeValue("ManagedInputRequest", value);
    const rpc = this.connection;
    if (this.phase !== "running" || rpc === undefined) {
      throw new SessionLifecycleError("Input requires the ready event of an active managed invocation; use explicit resume to reopen a session");
    }
    if (request.expected.run_id !== this.loaded.manifest.run_id || request.expected.trial_id !== this.selectedTrial ||
        request.expected.account_id !== this.loaded.manifest.account_id) {
      throw new SessionLifecycleError("Input belongs to another account/Run/Trial");
    }
    this.pendingInputs += 1;
    this.acceptedInputFence = true;
    try {
      const receipt = runtimeValue("ManagedInputReceipt", await rpc.call("managed_input", { ...request }));
      if (receipt.run_id !== request.expected.run_id || receipt.trial_id !== request.expected.trial_id || receipt.kind !== request.kind) {
        throw new Error("Managed input receipt differs from its caller request");
      }
      this.observe(await readRuntime(rpc, { run_id: receipt.run_id, trial_id: receipt.trial_id }));
      this.emit("input", receipt);
      return receipt;
    } finally { this.pendingInputs -= 1; }
  }

  prompt(request: ManagedInputRequest): Promise<ManagedInputReceipt> {
    return this.inputKind("prompt", request);
  }

  steer(request: ManagedInputRequest): Promise<ManagedInputReceipt> {
    return this.inputKind("steer", request);
  }

  followUp(request: ManagedInputRequest): Promise<ManagedInputReceipt> {
    return this.inputKind("follow_up", request);
  }

  async state(): Promise<RuntimeState> {
    this.assertOpen();
    const request = { run_id: this.loaded.manifest.run_id, trial_id: this.selectedTrial };
    if (this.connection !== undefined) return await readRuntime(this.connection, request);
    if (this.active !== undefined) throw new SessionLifecycleError("Runtime state is not ready; wait for ready or the invocation result");
    const cancellation = new AbortController();
    try {
      const signal = this.worker.signal === undefined ? cancellation.signal : AbortSignal.any([cancellation.signal, this.worker.signal]);
      const reading = runRuntimeWithWorker({ ...this.worker, signal }, async rpc => {
        const state = await readRuntime(rpc, request);
        const completion = await rpc.call("complete");
        if (completion.status !== "bootstrap_complete") throw new Error("Runtime state worker did not complete its bootstrap protocol");
        return { state };
      });
      this.reads.set(cancellation, reading);
      const result = await reading;
      const state = runtimeValue("RuntimeState", result.state);
      this.observe(state);
      return structuredClone(state);
    } finally { this.reads.delete(cancellation); }
  }

  interrupt(reason = "Caller interrupted this application session"): void {
    if (this.phase === "disposed" || this.cancellation === undefined || this.cancellation.signal.aborted) return;
    this.phase = "interrupted";
    this.connection = undefined;
    this.cancellation.abort(new SessionLifecycleError(reason));
    this.emit("interrupted", { reason, remoteCancellation: "unknown" });
  }

  dispose(): Promise<void> {
    if (this.disposal !== undefined) return this.disposal;
    this.interrupt("Application session disposed");
    this.phase = "disposed";
    this.listeners.clear();
    for (const read of this.reads.keys()) read.abort(new SessionLifecycleError("Application session disposed"));
    const operations = [...this.reads.values(), ...(this.active === undefined ? [] : [this.active])];
    this.disposal = Promise.allSettled(operations).then(() => undefined);
    return this.disposal;
  }

  fork(): never {
    throw new UnsupportedCapabilityError("Native fork is not qualified; select a separately admitted Run or explicit next Trial");
  }

  private async execute(options: RunOptions, signal: AbortSignal): Promise<SessionResult> {
    let generation: Promise<Record<string, unknown>> | undefined;
    try {
      const { resume, nextTrial } = await runRequests(options, this.loaded);
      signal.throwIfAborted();
      this.selectedTrial = nextTrial?.trial_id ?? resume?.expected.trial_id ?? this.selectedTrial;
      const result = await runRuntimeWithWorker({ ...this.worker, signal, manifest: this.manifestPath, manifestDigest: this.loaded.digest },
        async rpc => await runManaged(rpc, this.loaded,
          client => {
            generation = runGovernedPi(client, { signal, onEvent: event => this.emit("harness", event) });
            return generation;
          }, resume, nextTrial, {
            onState: state => this.observe(state),
            onReady: ({ rpc: bound, state }) => {
              signal.throwIfAborted();
              this.connection = bound;
              this.observe(state);
              this.emit("ready", state);
            },
            onGenerationStopped: () => { this.connection = undefined; },
          }));
      if (typeof result.status !== "string") throw new Error("Managed invocation returned no terminal status");
      const state = runtimeValue("RuntimeState", result.runtime_state);
      this.observe(state);
      const received: SessionResult = { ...result, status: result.status, runtime_state: state };
      if (this.phase !== "disposed") this.phase = "idle";
      this.emit("completed", received);
      return structuredClone(received);
    } catch (error) {
      if (this.phase !== "disposed") this.phase = signal.aborted ? "interrupted" : "failed";
      this.emit("failed", { message: error instanceof Error ? error.message : String(error), interrupted: signal.aborted });
      throw error;
    } finally {
      this.connection = undefined;
      // Worker cancellation can win its race before the governed child has closed.
      if (generation !== undefined) await Promise.allSettled([generation]);
    }
  }

  private inputKind(kind: ManagedInputRequest["kind"], request: ManagedInputRequest): Promise<ManagedInputReceipt> {
    if (request.kind !== kind) throw new SessionLifecycleError(`Expected ${kind} input, received ${request.kind}`);
    return this.input(request);
  }

  private release(operation: Promise<SessionResult>): void {
    if (this.active !== operation) return;
    this.active = undefined;
    this.cancellation = undefined;
  }

  private observe(state: RuntimeState): void {
    this.observation = structuredClone(state);
    this.selectedTrial = state.trial.trial_id;
    if (this.active !== undefined) this.emit("runtime", state);
  }

  private emit(type: SessionEvent["type"], payload: unknown): void {
    if (this.phase === "disposed") return;
    const event: SessionEvent = { applicationSessionId: this.applicationSessionId, invocationId: this.invocationId,
      sequence: ++this.sequence, type, payload };
    this.eventQueue.push(event);
    if (this.dispatching) return;
    this.dispatching = true;
    try {
      while (this.eventQueue.length > 0) this.dispatch(this.eventQueue.shift()!);
    } finally { this.dispatching = false; }
  }

  private dispatch(event: SessionEvent): void {
    const failures: string[] = [];
    for (const listener of this.listeners) {
      try { listener(structuredClone(event)); }
      catch (error) {
        this.listeners.delete(listener);
        failures.push(error instanceof Error ? error.message : String(error));
      }
    }
    if (failures.length > 0) this.emit("observer_error", { messages: failures });
  }

  private assertOpen(): void {
    if (this.phase === "disposed") throw new SessionLifecycleError("Application session is disposed");
  }
}
