import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { controlledContext } from "./context-control.ts";
import { fork } from "node:child_process";
import type { ChildProcess } from "node:child_process";
import { delimiter, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import type { EffectRpc } from "../clients/rpc.ts";
import type { HarnessRunner, HarnessOptions, HarnessEvent } from "../harness/types.ts";
import { DEFAULT_MAX_PAYLOAD_BYTES } from "../clients/rpc-payload.ts";
import { controlledRsi } from "./rsi-control.ts";
import { controlledModels } from "./model-control.ts";

function object(value: unknown): Record<string, unknown> {
  if (value === null || typeof value !== "object" || Array.isArray(value)) throw new Error("Invalid Pi channel object");
  if (Buffer.byteLength(JSON.stringify(value)) > DEFAULT_MAX_PAYLOAD_BYTES) throw new Error("Pi channel exceeds payload limit");
  return value as Record<string, unknown>;
}

function send(child: ChildProcess, value: object): Promise<void> {
  return new Promise((resolve, reject) => {
    if (!child.connected) { reject(new Error("Pi channel disconnected; inspect original receipts")); return; }
    child.send(value, (error) => error ? reject(error) : resolve());
  });
}

async function stop(child: ChildProcess, exited: Promise<void>): Promise<void> {
  if (!child.pid || child.exitCode !== null || child.signalCode !== null) return;
  if (child.connected) child.disconnect();
  let terminationError: unknown;
  const timer = setTimeout(() => {
    try { child.kill("SIGKILL"); }
    catch (error) { terminationError = error; }
  }, 1000);
  let deadline: ReturnType<typeof setTimeout> | undefined;
  try {
    await Promise.race([exited, new Promise<never>((_resolve, reject) => {
      deadline = setTimeout(() => {
        child.unref();
        child.stderr?.destroy();
        reject(new Error("Pi exit unconfirmed; original effects require reconciliation", { cause: terminationError }));
      }, 2000);
    })]);
  } finally {
    clearTimeout(timer);
    clearTimeout(deadline);
  }
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<T>((accept, refuse) => { resolve = accept; reject = refuse; });
  // Lifecycle failures can arrive before the caller starts awaiting the result.
  void promise.catch(() => {});
  return { promise, resolve, reject };
}

class PiProcess {
  private readonly child: ChildProcess;
  private readonly ready = deferred<void>();
  private readonly result = deferred<Record<string, unknown>>();
  private readonly exited: Promise<void>;
  private readonly startup: ReturnType<typeof setTimeout>;
  private rpc?: EffectRpc;
  private expectedId = 1;
  private expectedEventId = 1;
  private onEvent?: (event: HarnessEvent) => void;
  private busy = false;
  private stderr = "";
  private phase: "loading" | "ready" | "running" | "done" | "failed" = "loading";

  private readonly options: HarnessOptions;

  constructor(options: HarnessOptions = {}) {
    this.options = options;
    options.signal?.throwIfAborted();
    this.child = fork(fileURLToPath(new URL("../harness/pi/process.ts", import.meta.url)), [], {
      execPath: process.execPath, execArgv: [], serialization: "json",
      env: { PATH: [dirname(process.execPath), "/usr/bin", "/bin"].join(delimiter), LANG: "C.UTF-8" },
      stdio: ["ignore", "ignore", "pipe", "ipc"],
    });
    this.startup = setTimeout(() => this.fail(new Error("Pi startup timed out before execution")), 30000);
    this.child.on("error", (error) => this.fail(error));
    this.child.stderr?.on("data", (chunk: Buffer) => {
      this.stderr = (this.stderr + chunk.toString("utf8")).slice(-65536);
    });
    this.exited = new Promise<void>((resolve) => this.child.once("exit", (code, signal) => {
      if (this.phase !== "done") this.fail(new Error(`Pi exited (${signal ?? code}); reconcile original receipts before continuing\n${this.stderr}`));
      resolve();
    }));
    options.signal?.addEventListener("abort", this.interrupted, { once: true });
    if (options.signal?.aborted) this.interrupted();
    process.on("SIGTERM", this.interrupted);
    process.on("SIGINT", this.interrupted);
    this.child.on("message", (raw: unknown) => { void this.receive(raw).catch((error) => this.fail(error)); });
  }

  private readonly interrupted = (): void => {
    this.fail(new Error("LoopX host interrupted; Pi effects require reconciliation"));
  };

  private fail(error: unknown): void {
    this.phase = "failed";
    const cause = error instanceof Error ? error : new Error(String(error));
    this.ready.reject(cause);
    this.result.reject(cause);
  }

  private async receive(raw: unknown): Promise<void> {
    const message = object(raw);
    if (message.type === "ready" && this.phase === "loading") {
      this.phase = "ready";
      clearTimeout(this.startup);
      this.ready.resolve();
    } else if (message.type === "event" && this.phase === "running") {
      const event = object(message.event);
      if (message.sequence !== this.expectedEventId++ || typeof event.type !== "string") {
        throw new Error("Invalid Pi observation sequence or event type");
      }
      this.onEvent?.(structuredClone(event) as HarnessEvent);
    } else if (message.type === "request" && this.phase === "running" && this.rpc && !this.busy) {
      if (message.id !== this.expectedId++ || typeof message.method !== "string") throw new Error("Invalid Pi request sequence");
      this.busy = true;
      const response = await this.rpc.call(message.method, object(message.params));
      await send(this.child, { type: "reply", id: message.id, result: object(response) });
      this.busy = false;
    } else if (message.type === "result" && this.phase === "running" && !this.busy) {
      this.phase = "done";
      this.result.resolve(object(message.result));
    } else if (message.type === "error" && typeof message.message === "string") {
      throw new Error(message.message);
    } else {
      throw new Error("Unexpected Pi lifecycle message");
    }
  }

  async prepare(): Promise<void> { await this.ready.promise; }

  async run(client: EffectRpc, directory: string, options: HarnessOptions = {}): Promise<Record<string, unknown>> {
    if (this.phase !== "ready") throw new Error("Prepared Pi process can execute only once");
    this.onEvent = options.onEvent;
    this.rpc = controlledContext(controlledRsi(controlledModels(client)), directory);
    this.phase = "running";
    await send(this.child, { type: "start" });
    return await this.result.promise;
  }

  async close(): Promise<void> {
    clearTimeout(this.startup);
    this.options.signal?.removeEventListener("abort", this.interrupted);
    process.removeListener("SIGTERM", this.interrupted);
    process.removeListener("SIGINT", this.interrupted);
    await stop(this.child, this.exited);
  }
}

/** LoopX's trusted host owns policy; the child loads only Pi and forwarding hooks. */
export async function withPreparedPi<T>(execute: (run: HarnessRunner) => Promise<T>, options: HarnessOptions = {}): Promise<T> {
  options.signal?.throwIfAborted();
  const directory = await mkdtemp(join(tmpdir(), "loopx-context-"));
  let prepared: PiProcess | undefined;
  try {
    const process = new PiProcess(options);
    prepared = process;
    await process.prepare();
    return await execute((client, runOptions) => process.run(client, directory, runOptions));
  } finally {
    try { await prepared?.close(); }
    finally { await rm(directory, { recursive: true, force: true }); }
  }
}

export async function runGovernedPi(rpc: EffectRpc, options: HarnessOptions = {}): Promise<Record<string, unknown>> {
  return await withPreparedPi((run) => run(rpc, options), options);
}
