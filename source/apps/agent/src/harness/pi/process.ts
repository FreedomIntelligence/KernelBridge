import type { EffectRpc } from "../../clients/rpc.ts";
import { withPreparedAgent } from "./sdk.mjs";

// This channel carries lifecycle RPC only. Domain requests retain Worker validation.
let nextId = 1;
let eventSequence = 1;
let pending: { id: number; resolve(value: Record<string, unknown>): void; reject(error: Error): void } | null = null;
let start: (() => void) | undefined;
let stopped = false;

function send(message: object): void {
  if (!process.send || !process.connected) throw new Error("Pi lost its controlling process");
  process.send(message, (error) => {
    if (error) {
      stopped = true;
      pending?.reject(error);
    }
  });
}

const rpc: EffectRpc = {
  call(method, params = {}) {
    if (stopped || pending !== null) return Promise.reject(new Error("Pi channel stopped or busy; no retry"));
    const id = nextId++;
    return new Promise((resolve, reject) => {
      pending = { id, resolve, reject };
      try { send({ type: "request", id, method, params }); }
      catch (error) { stopped = true; reject(error); }
    });
  },
};

process.on("message", (message: unknown) => {
  if (message === null || typeof message !== "object" || Array.isArray(message)) return;
  const value = message as Record<string, unknown>;
  if (value.type === "start" && start) {
    const run = start;
    start = undefined;
    run();
  } else if (value.type === "reply" && pending && value.id === pending.id
      && value.result !== null && typeof value.result === "object" && !Array.isArray(value.result)) {
    const request = pending;
    pending = null;
    request.resolve(value.result as Record<string, unknown>);
  } else {
    stopped = true;
    pending?.reject(new Error("Invalid controlling-process reply"));
  }
});
process.on("disconnect", () => process.exit(1));

try {
  const result = await withPreparedAgent(async (run) => {
    await new Promise<void>((resolve) => { start = resolve; send({ type: "ready" }); });
    return await run(rpc, { onEvent: event => send({ type: "event", sequence: eventSequence++, event }) });
  });
  send({ type: "result", result });
} catch (error) {
  send({ type: "error", message: error instanceof Error ? error.message : String(error) });
}
