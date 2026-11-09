import { parseArgs, TextDecoder } from "node:util";
import { runLoopXWithWorker } from "../clients/worker-client.ts";
import { executeLoopXHost } from "./host.ts";

async function readRequest(): Promise<string> {
  const chunks: Buffer[] = [];
  let bytes = 0;
  for await (const chunk of process.stdin) {
    bytes += chunk.length;
    if (bytes > 1048576) throw new Error("LoopX request exceeds its byte bound");
    chunks.push(Buffer.from(chunk));
  }
  return new TextDecoder("utf-8", { fatal: true }).decode(Buffer.concat(chunks));
}

try {
  const { values } = parseArgs({ options: {
    python: { type: "string" }, "expected-python-modules": { type: "string" },
    plan: { type: "string" }, "timeout-ms": { type: "string" }, "max-frame-bytes": { type: "string" },
  }, allowPositionals: false });
  const required = (name: keyof typeof values): string => {
    const value = values[name];
    if (typeof value !== "string" || value.length === 0) throw new Error(`Missing ${name}`);
    return value;
  };
  const request = await readRequest();
  const result = await runLoopXWithWorker({
    python: required("python"), expectedPythonModules: required("expected-python-modules"),
    plan: required("plan"), timeoutMs: Number(required("timeout-ms")),
    maxFrameBytes: Number(required("max-frame-bytes")),
  }, (rpc) => executeLoopXHost(rpc, request));
  process.stdout.write(JSON.stringify(result) + "\n");
} catch {
  process.stderr.write("KernelCat LoopX host stopped; inspect original receipts before resuming.\n");
  process.exitCode = 2;
}
