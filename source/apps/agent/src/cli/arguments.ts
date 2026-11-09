import { DEFAULT_WORKER_FRAME_BYTES, DEFAULT_WORKER_PAYLOAD_BYTES } from "../clients/worker-client.ts";
import type { PythonProcessOptions, WorkerConnectionOptions, WorkerOptions } from "../clients/worker-client.ts";
import { isAbsolute } from "node:path";

export type ProductOptions =
  | { readonly command: "pursue"; readonly goal: string }
  | (PythonProcessOptions & { readonly command: "loopx-cycle"; readonly plan: string; readonly execute: boolean })
  | (WorkerOptions & { readonly command: "run"; readonly mode: "legacy" })
  | (WorkerOptions & { readonly command: "rsi-collect"; readonly actionId: string })
  | (WorkerOptions & { readonly command: "rsi-replay" })
  | (WorkerConnectionOptions & { readonly command: "run"; readonly mode: "managed"; readonly manifest: string; readonly resume?: string; readonly nextTrial?: string })
  | (WorkerConnectionOptions & { readonly command: "account-create"; readonly intentId: string; readonly credits: number; readonly knowledgeAuthorityId?: string; readonly multiTrial?: true })
  | (WorkerConnectionOptions & { readonly command: "runtime-state"; readonly runId: string; readonly trialId: string })
  | (WorkerConnectionOptions & { readonly command: "knowledge-sync"; readonly runId: string; readonly trialId: string; readonly limit?: number })
  | (WorkerConnectionOptions & { readonly command: "stop-run" | "knowledge-fence"; readonly request: string });

export const HELP = `Usage: node src/main.ts COMMAND --python ABS --config ABS --expected-python-modules ABS [OPTIONS]

  account-create --intent ID --credits N   Explicitly create one shared account
    [--knowledge-authority SHA256]        Explicit fresh v2 Knowledge authority
    [--multi-trial]                       Explicit fresh v3 account
  run --manifest ABS [--resume ABS | --next-trial ABS]
                                         Execute one explicit managed Trial invocation
  pursue --goal ABS                       Automatic managed Trials through LoopX only
                                         Binding supplies Worker and existing Goal; no common flags
  run --task ABS                          Explicit legacy task mode
  runtime-state --run-id ID --trial-id ID  Read current Run/Trial authority
  stop-run --request ABS                  Submit a stable StopRunRequest file
  knowledge-fence --request ABS           Submit a stable KnowledgeFenceRequest
  knowledge-sync --run ID --trial ID [--limit 1..100]
                                         Reconcile this original consumer only
  loopx-cycle --plan ABS [--execute]       Preview or run bounded official LoopX Turns
                                         Config/task are bound by the plan; omit --config
  rsi-collect --task ABS --action-id ID    Collect an original admitted experiment job
                                         Starts no Pi session or new experiment
  rsi-replay --task ABS                   Apply an original Agent replay request once
                                         Starts no model or device experiment

All file paths and the Python executable must be absolute.
--expected-python-modules selects an independently prepared readable binding.
--resume reads a stable ReplaceSessionRequest; loss never mints or retries one.
Account/control commands start no Pi session or model request.
Optional --timeout-ms N defaults to 300000; effects are never retried after loss.
--max-frame-bytes N and --max-payload-bytes N bound the finite transport.
Use --help to print this help without starting a worker.
`;

function required(values: ReadonlyMap<string, string>, name: string): string {
  const value = values.get(name);
  if (value === undefined) throw new Error(`Required argument: ${name}`);
  return value;
}

function argumentValues(args: readonly string[], allowed: ReadonlySet<string>): ReadonlyMap<string, string> {
  const values = new Map<string, string>();
  for (let index = 1; index < args.length; index += 1) {
    const name = args[index];
    if (!allowed.has(name) || values.has(name)) throw new Error(`Invalid or repeated argument: ${name}`);
    if (name === "--multi-trial" || name === "--execute") {
      values.set(name, "true");
      continue;
    }
    const value = args[index + 1];
    if (value === undefined || value.startsWith("--")) throw new Error(`Missing value for argument: ${name}`);
    values.set(name, value);
    index += 1;
  }
  for (const field of ["--python", "--config", "--expected-python-modules", "--task", "--manifest", "--resume", "--next-trial", "--request", "--plan", "--goal"]) {
    const value = values.get(field);
    if (value !== undefined && !isAbsolute(value)) throw new Error(`${field} must be an absolute path`);
  }
  return values;
}

export function parseArguments(args: readonly string[]): ProductOptions | null {
  if (args.length === 0 || args.includes("--help") || args.includes("-h")) return null;
  const command = args[0];
  if (command === "pursue") {
    const values = argumentValues(args, new Set(["--goal"]));
    return { command, goal: required(values, "--goal") };
  }
  const specific = {
    run: ["--task", "--manifest", "--resume", "--next-trial"],
    "account-create": ["--intent", "--credits", "--knowledge-authority", "--multi-trial"],
    "runtime-state": ["--run-id", "--trial-id"],
    "stop-run": ["--request"],
    "knowledge-fence": ["--request"],
    "knowledge-sync": ["--run", "--trial", "--limit"],
    "loopx-cycle": ["--plan", "--execute"],
    "rsi-collect": ["--task", "--action-id"],
    "rsi-replay": ["--task"],
  };
  if (!Object.hasOwn(specific, command)) throw new Error("Expected run or an account/control command; use --help");
  const allowed = new Set(["--python", "--expected-python-modules", "--timeout-ms", "--max-frame-bytes", "--max-payload-bytes",
    ...(command === "loopx-cycle" ? [] : ["--config"]),
    ...specific[command as keyof typeof specific]]);
  const values = argumentValues(args, allowed);
  const processOptions = {
    python: required(values, "--python"),
    expectedPythonModules: required(values, "--expected-python-modules"),
    timeoutMs: Number(values.get("--timeout-ms") ?? 300000),
    maxFrameBytes: Number(values.get("--max-frame-bytes") ?? DEFAULT_WORKER_FRAME_BYTES),
    maxPayloadBytes: Number(values.get("--max-payload-bytes") ?? DEFAULT_WORKER_PAYLOAD_BYTES),
  };
  if (command === "loopx-cycle") return { ...processOptions, command,
    plan: required(values, "--plan"), execute: values.has("--execute") };
  const common = { ...processOptions, config: required(values, "--config") };
  if (command === "rsi-replay") return { ...common, command, task: required(values, "--task") };
  if (command === "rsi-collect") return { ...common, command,
    task: required(values, "--task"), actionId: required(values, "--action-id") };
  if (command === "account-create") return { ...common, command, intentId: required(values, "--intent"),
    credits: Number(required(values, "--credits")),
    ...(values.has("--knowledge-authority") ? { knowledgeAuthorityId: values.get("--knowledge-authority")! } : {}),
    ...(values.has("--multi-trial") ? { multiTrial: true } : {}) };
  if (command === "runtime-state") return { ...common, command, runId: required(values, "--run-id"), trialId: required(values, "--trial-id") };
  if (command === "knowledge-sync") {
    const limit = values.get("--limit");
    if (limit !== undefined && (!/^[0-9]+$/.test(limit) || Number(limit) < 1 || Number(limit) > 100)) {
      throw new Error("--limit must be an integer between 1 and 100");
    }
    return { ...common, command, runId: required(values, "--run"), trialId: required(values, "--trial"),
      ...(limit === undefined ? {} : { limit: Number(limit) }) };
  }
  if (command === "stop-run" || command === "knowledge-fence") return { ...common, command, request: required(values, "--request") };
  return runOptions(common, values);
}

function runOptions(common: WorkerConnectionOptions, values: ReadonlyMap<string, string>): ProductOptions {
  if (values.has("--manifest")) {
    if (values.has("--task")) throw new Error("Choose exactly one of --manifest and explicit legacy --task");
    if (values.has("--resume") && values.has("--next-trial")) throw new Error("Choose exactly one of --resume and --next-trial");
    return { ...common, command: "run", mode: "managed", manifest: required(values, "--manifest"),
      ...(values.has("--resume") ? { resume: values.get("--resume")! } : {}),
      ...(values.has("--next-trial") ? { nextTrial: values.get("--next-trial")! } : {}) };
  }
  if (values.has("--resume")) throw new Error("--resume requires --manifest");
  if (values.has("--next-trial")) throw new Error("--next-trial requires --manifest");
  return { ...common, command: "run", mode: "legacy", task: required(values, "--task") };
}
