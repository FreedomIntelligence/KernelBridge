import { spawn } from "node:child_process";
import type { ChildProcessWithoutNullStreams } from "node:child_process";
import { access } from "node:fs/promises";
import { constants } from "node:fs";
import { isAbsolute } from "node:path";
import { fileURLToPath } from "node:url";
import { RpcClient, DEFAULT_FRAME_BYTES, frames } from "./protocol.mjs";
import type { EffectRpc } from "./rpc.ts";
import { DEFAULT_MAX_PAYLOAD_BYTES, validatePayloadLimit } from "./rpc-payload.ts";

const PYTHON_STARTUP = fileURLToPath(new URL("../../python/generated_startup.py", import.meta.url));
const ACTIVE_AGENT_ROOT = fileURLToPath(new URL("../../", import.meta.url));

export interface PythonProcessOptions {
  readonly signal?: AbortSignal;
  readonly python: string;
  readonly expectedPythonModules: string;
  readonly timeoutMs: number;
  readonly maxFrameBytes: number;
  readonly stderrBytes?: number;
  readonly maxPayloadBytes?: number;
}

export interface WorkerConnectionOptions extends PythonProcessOptions {
  readonly config: string;
}

export interface WorkerOptions extends WorkerConnectionOptions {
  readonly task: string;
}

export interface RuntimeWorkerOptions extends WorkerConnectionOptions {
  readonly manifest?: string;
  readonly manifestDigest?: string;
}

export interface LoopXWorkerOptions extends PythonProcessOptions {
  readonly plan: string;
}

export class WorkerFailure extends Error {}

async function validateOptions(options: PythonProcessOptions): Promise<void> {
  validatePayloadLimit(options.maxPayloadBytes ?? DEFAULT_MAX_PAYLOAD_BYTES);
  for (const field of ["python", "expectedPythonModules"] as const) {
    if (typeof options[field] !== "string" || !isAbsolute(options[field])) throw new Error(`${field} must be an absolute path`);
    await access(options[field], field === "python" ? constants.X_OK : constants.R_OK);
  }
  if (!Number.isSafeInteger(options.timeoutMs) || options.timeoutMs < 1 || options.timeoutMs > 86400000) {
    throw new Error("Worker timeout must be between 1 and 86400000 milliseconds");
  }
  if (!Number.isSafeInteger(options.maxFrameBytes) || options.maxFrameBytes < 4096 || options.maxFrameBytes > 16777216) {
    throw new Error("Worker frame limit must be between 4096 and 16777216 bytes");
  }
  if (options.stderrBytes !== undefined && (!Number.isSafeInteger(options.stderrBytes) || options.stderrBytes < 1 || options.stderrBytes > 1048576)) {
    throw new Error("Worker stderr limit must be between 1 and 1048576 bytes");
  }
}

interface WorkerExit {
  readonly code: number | null;
  readonly signal: NodeJS.Signals | null;
  readonly error?: Error;
}

interface WorkerStatus {
  readonly exited: Promise<WorkerExit>;
  readonly closed: Promise<void>;
  readonly errors: readonly Error[];
  hasExited(): boolean;
  signal(signal: NodeJS.Signals): void;
  dispose(): void;
}

function observeWorker(child: ChildProcessWithoutNullStreams): WorkerStatus {
  let hasExited = false;
  let activeSignal: NodeJS.Signals | null = null;
  let resolveExit: (exit: WorkerExit) => void;
  const errors: Error[] = [];
  const exited = new Promise<WorkerExit>((resolve) => {
    resolveExit = resolve;
  });
  const onError = (error: Error): void => {
    if (!child.pid) {
      hasExited = true;
      resolveExit({ code: null, signal: null, error });
      return;
    }
    const operation = activeSignal === null ? "process" : `${activeSignal} delivery`;
    errors.push(new WorkerFailure(`Python worker ${operation} failed: ${error.message}`, { cause: error }));
  };
  // kill() may emit an error for each failed delivery, including during cleanup.
  child.on("error", onError);
  child.once("exit", (code, signal) => {
    hasExited = true;
    resolveExit({ code, signal });
  });
  const closed = new Promise<void>((resolve) => child.once("close", resolve));
  return {
    exited, closed, errors, hasExited: () => hasExited,
    signal(signal): void {
      const priorErrors = errors.length;
      activeSignal = signal;
      try {
        if (!child.kill(signal) && !hasExited && errors.length === priorErrors) {
          errors.push(new WorkerFailure(`Python worker ${signal} delivery failed`));
        }
      } catch (error) {
        onError(error instanceof Error ? error : new Error(String(error), { cause: error }));
      } finally {
        activeSignal = null;
      }
    },
    dispose(): void {
      child.removeListener("error", onError);
    },
  };
}

interface StopBoundary {
  readonly failure: Promise<never>;
  reason(): WorkerFailure | null;
  terminationSent(): boolean;
  terminate(): void;
  dispose(): void;
}

function stopBoundary(status: WorkerStatus, timeoutMs: number, signal?: AbortSignal): StopBoundary {
  let reason: WorkerFailure | null = null;
  let terminationSent = false;
  let rejectStop: (error: WorkerFailure) => void;
  const failure = new Promise<never>((_, reject) => { rejectStop = reject; });
  // A synchronous client failure can enter cleanup before the main race is set up.
  void failure.catch(() => {});
  const terminate = (): void => {
    if (!terminationSent) {
      terminationSent = true;
      status.signal("SIGTERM");
    }
  };
  const stop = (error: WorkerFailure): void => {
    if (reason !== null) return;
    reason = error;
    rejectStop(error);
    terminate();
  };
  const onTerm = (): void => stop(new WorkerFailure("Node owner received SIGTERM; inspect receipts before resuming"));
  const onInt = (): void => stop(new WorkerFailure("Node owner received SIGINT; inspect receipts before resuming"));
  const onAbort = (): void => stop(new WorkerFailure("Caller interrupted Python worker; inspect receipts before resuming", { cause: signal?.reason }));
  signal?.addEventListener("abort", onAbort, { once: true });
  if (signal?.aborted) onAbort();
  process.on("SIGTERM", onTerm);
  process.on("SIGINT", onInt);
  const timer = setTimeout(() => stop(new WorkerFailure("Python worker timed out")), timeoutMs);
  return {
    failure, reason: () => reason, terminationSent: () => terminationSent, terminate,
    dispose(): void {
      clearTimeout(timer);
      signal?.removeEventListener("abort", onAbort);
      process.removeListener("SIGTERM", onTerm);
      process.removeListener("SIGINT", onInt);
    },
  };
}

async function settledWithin(promise: Promise<unknown>, milliseconds: number): Promise<boolean> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    return await Promise.race([
      promise.then(() => true),
      new Promise<boolean>((resolve) => { timer = setTimeout(() => resolve(false), milliseconds); }),
    ]);
  } finally {
    if (timer) clearTimeout(timer);
  }
}

async function closePipes(child: ChildProcessWithoutNullStreams, status: WorkerStatus): Promise<void> {
  // Descendants may retain inherited pipes after the direct worker exits.
  // Drain only briefly, then release our handles without asserting their effects stopped.
  await settledWithin(status.closed, 50);
  child.stdin.destroy();
  child.stdout.destroy();
  child.stderr.destroy();
}

async function shutdownWorker(
  child: ChildProcessWithoutNullStreams, status: WorkerStatus, boundary: StopBoundary,
): Promise<boolean> {
  try {
    child.stdin.end();
    if (!status.hasExited() && !boundary.terminationSent()) {
      await settledWithin(status.exited, 250);
    }
    if (!status.hasExited()) {
      boundary.terminate();
      if (!await settledWithin(status.exited, 1000)) {
        status.signal("SIGKILL");
        await settledWithin(status.exited, 250);
      }
    }
  } finally {
    await closePipes(child, status);
    // Failed delivery releases only our handle; effects still need reconciliation.
    if (!status.hasExited()) {
      child.unref();
    }
  }
  return status.hasExited();
}

function checkWorkerExit(exit: WorkerExit): void {
  if (exit.error) throw new WorkerFailure("Could not start Python worker", { cause: exit.error });
  if (exit.code !== 0) {
    throw new WorkerFailure(`Python worker exited with ${exit.signal ? `signal ${exit.signal}` : `code ${exit.code}`}`);
  }
}

/** One child, one RPC stream, one Pi loop. Loss and timeout never redispatch effects. */
export async function runWithWorker(
  options: WorkerOptions,
  execute: (rpc: EffectRpc) => Promise<Record<string, unknown>>,
): Promise<Record<string, unknown>> {
  if (!isAbsolute(options.task)) throw new Error("task must be an absolute path");
  await access(options.task, constants.R_OK);
  return await withWorker(options, ["serve", "--config", options.config, "--task", options.task], execute);
}

export async function runRuntimeWithWorker(
  options: RuntimeWorkerOptions,
  execute: (rpc: EffectRpc) => Promise<Record<string, unknown>>,
): Promise<Record<string, unknown>> {
  const arguments_ = ["serve-runtime", "--config", options.config];
  if (options.manifest !== undefined) {
    if (!isAbsolute(options.manifest)) throw new Error("manifest must be an absolute path");
    await access(options.manifest, constants.R_OK);
    if (!/^sha256:[a-f0-9]{64}$/.test(options.manifestDigest ?? "")) {
      throw new Error("Managed Worker requires the exact manifest byte digest");
    }
    arguments_.push("--manifest", options.manifest);
  } else if (options.manifestDigest !== undefined) {
    throw new Error("Manifest digest requires a manifest path");
  }
  return await withWorker(options, arguments_, execute);
}

/** The Node LoopX host owns Pi; Python admits only finite round operations. */
export async function runLoopXWithWorker(
  options: LoopXWorkerOptions,
  execute: (rpc: EffectRpc) => Promise<Record<string, unknown>>,
): Promise<Record<string, unknown>> {
  if (!isAbsolute(options.plan)) throw new Error("LoopX plan must be an absolute path");
  await access(options.plan, constants.R_OK);
  return await withPythonProcess(options, "loopx-host", ["--plan", options.plan], (child) => execute(
    new RpcClient(child.stdout, child.stdin, options.maxFrameBytes, options.maxPayloadBytes)));
}

async function withWorker(
  options: RuntimeWorkerOptions,
  arguments_: readonly string[],
  execute: (rpc: EffectRpc) => Promise<Record<string, unknown>>,
): Promise<Record<string, unknown>> {
  if (!isAbsolute(options.config)) throw new Error("config must be an absolute path");
  await access(options.config, constants.R_OK);
  return await withPythonProcess(options, "worker", arguments_, (child) => execute(
    new RpcClient(child.stdout, child.stdin, options.maxFrameBytes, options.maxPayloadBytes)), options);
}

/** Finite administration reuses the same process lifetime and no-retry boundary. */
export async function runAdministration(
  options: PythonProcessOptions, arguments_: readonly string[],
): Promise<Record<string, unknown>> {
  return await withPythonProcess(options, "worker-admin", arguments_, async (child) => {
    child.stdin.end();
    let result: Record<string, unknown> | undefined;
    for await (const value of frames(child.stdout, options.maxFrameBytes)) {
      if (result !== undefined || value === null || typeof value !== "object" || Array.isArray(value)) {
        throw new Error("Administration requires exactly one JSON object");
      }
      result = value as Record<string, unknown>;
    }
    if (result === undefined) throw new Error("Administration returned no result");
    return result;
  });
}

async function withPythonProcess(
  options: PythonProcessOptions, mode: "worker" | "worker-admin" | "loopx-host", arguments_: readonly string[],
  execute: (child: ChildProcessWithoutNullStreams) => Promise<Record<string, unknown>>,
  runtime: Pick<RuntimeWorkerOptions, "manifest" | "manifestDigest"> = {},
): Promise<Record<string, unknown>> {
  options.signal?.throwIfAborted();
  await validateOptions(options);
  options.signal?.throwIfAborted();
  const child = spawn(options.python, [
    "-B", "-P", PYTHON_STARTUP, "--expected-python-modules", options.expectedPythonModules,
    mode, ...arguments_,
  ], {
    shell: false,
    stdio: ["pipe", "pipe", "pipe"],
    env: {
      ...process.env,
      PYTHONUNBUFFERED: "1",
      KERNELCAT_MAX_FRAME_BYTES: String(options.maxFrameBytes),
      // Bind admitted hashes to this application, overriding caller-supplied identities.
      KERNELCAT_ACTIVE_AGENT_ROOT: ACTIVE_AGENT_ROOT,
      KERNELCAT_ACTIVE_NODE: process.execPath,
      KERNELCAT_ACTIVE_RUN_MANIFEST_PATH: runtime.manifest,
      KERNELCAT_ACTIVE_RUN_MANIFEST_SHA256: runtime.manifestDigest,
    },
  });
  const status = observeWorker(child);
  const boundary = stopBoundary(status, options.timeoutMs, options.signal);
  const stderrLimit = options.stderrBytes ?? 32768;
  let stderr = Buffer.alloc(0);
  child.stderr.on("data", (chunk: Buffer) => { stderr = Buffer.concat([stderr, chunk]).subarray(-stderrLimit); });
  child.stdin.on("error", () => {}); // RpcClient receives the write callback failure.
  try {
    if (boundary.reason()) throw boundary.reason();
    const execution = execute(child);
    const result = await Promise.race([execution, boundary.failure]);
    child.stdin.end();
    const exit = await Promise.race([status.exited, boundary.failure]);
    const controlledStop = mode === "worker-admin" && exit.code === 3 && exit.signal === null
      && (["blocked", "stopped", "deadline", "cancelled", "cycle_error"].includes(String(result.status))
        || arguments_[0] === "rsi-collect" && ["unknown", "failed"].includes(String(result.status)));
    if (!controlledStop) checkWorkerExit(exit);
    await closePipes(child, status);
    if (boundary.reason()) throw boundary.reason();
    return result;
  } catch (error) {
    const exitConfirmed = await shutdownWorker(child, status, boundary);
    const failure = boundary.reason() ?? error;
    const reason = failure instanceof Error ? failure.message : String(failure);
    const tail = stderr.toString("utf8").trim();
    const cleanup = exitConfirmed ? "" : "; direct worker exit unconfirmed";
    const diagnostics = status.errors.map(error => `; ${error.message}`).join("");
    const cause = status.errors.length === 0 ? error : new AggregateError([error, ...status.errors], "Python worker cleanup failed");
    throw new WorkerFailure(`${reason}; no automatic retry${cleanup}${diagnostics}${tail ? `\n${tail}` : ""}`, { cause });
  } finally {
    boundary.dispose();
    status.dispose();
  }
}

export const DEFAULT_WORKER_FRAME_BYTES = DEFAULT_FRAME_BYTES;
export const DEFAULT_WORKER_PAYLOAD_BYTES = DEFAULT_MAX_PAYLOAD_BYTES;
