import { spawn } from "node:child_process";
import { access, realpath } from "node:fs/promises";
import { constants } from "node:fs";
import { dirname, basename, join, isAbsolute } from "node:path";

export type JsonObject = Record<string, unknown>;
export interface LoopXConnection {
  readonly cli: string;
  readonly node: string;
  readonly project: string;
  readonly registry: string;
  readonly runtimeRoot: string;
  readonly home: string;
  readonly timeoutMs: number;
  readonly maxOutputBytes?: number;
}
export class LoopXFailure extends Error {
  readonly outcome: "unknown" | "rejected";
  readonly observation: JsonObject | undefined;
  constructor(message: string, outcome: "unknown" | "rejected", observation?: JsonObject) {
    super(message);
    this.outcome = outcome;
    this.observation = observation;
  }
}
export function object(value: unknown, label: string): JsonObject {
  if (value === null || typeof value !== "object" || Array.isArray(value)) throw new Error(`Invalid ${label}`);
  return value as JsonObject;
}
export function textField(value: JsonObject, key: string): string {
  const result = value[key];
  if (typeof result !== "string" || !result) throw new Error(`LoopX omitted ${key}`);
  return result;
}
export class LoopXClient {
  readonly options: Readonly<LoopXConnection>;
  private constructor(options: LoopXConnection) { this.options = Object.freeze({ ...options }); }
  static async connect(options: LoopXConnection, allowNewRegistry = false): Promise<LoopXClient> {
    if (!Number.isSafeInteger(options.timeoutMs) || options.timeoutMs < 1 || options.timeoutMs > 3_600_000) {
      throw new Error("LoopX timeout must be 1..3600000 milliseconds");
    }
    const maximum = options.maxOutputBytes ?? 4_194_304;
    if (!Number.isSafeInteger(maximum) || maximum < 4096 || maximum > 16_777_216) throw new Error("Invalid LoopX output bound");
    const canonical = { ...options };
    for (const key of ["cli", "node", "project", "registry", "runtimeRoot", "home"] as const) {
      if (!isAbsolute(options[key])) throw new Error(`LoopX ${key} must be an absolute path`);
      if (key === "registry" && allowNewRegistry) {
        canonical[key] = join(await realpath(dirname(options[key])), basename(options[key]));
      } else {
        await access(options[key], key === "cli" || key === "node" ? constants.X_OK : constants.R_OK);
        canonical[key] = await realpath(options[key]);
      }
    }
    const client = new LoopXClient(canonical);
    const version = await client.call(["version"]);
    if (version.name !== "loopx" || version.version !== "1.2.3" || version.schema_version !== "loopx_version_v0") {
      throw new Error("LoopX requires the qualified 1.2.3 installation");
    }
    return client;
  }
  async call(args: readonly string[], signal?: AbortSignal, observeRejection = false): Promise<JsonObject> {
    if (signal?.aborted) throw new LoopXFailure("LoopX cancelled before dispatch", "rejected");
    const o = this.options;
    // Explicit isolated HOME and executable paths; no provider keys or ambient SDK configuration.
    const env = { HOME: o.home, PATH: [dirname(o.node), dirname(o.cli), "/usr/bin", "/bin"].join(":"),
      PYTHONNOUSERSITE: "1", PYTHONDONTWRITEBYTECODE: "1" };
    return await new Promise((resolve, reject) => {
      const child = spawn(o.cli, ["--registry", o.registry, "--runtime-root", o.runtimeRoot,
        "--format", "json", ...args], { cwd: o.project, env, stdio: ["ignore", "pipe", "pipe"] });
      let stdout = "";
      let bytes = 0;
      let failure: Error | undefined;
      const stop = (): void => {
        failure ??= new LoopXFailure("LoopX outcome unknown; inspect original Turn before further work", "unknown");
        child.kill("SIGKILL");
      };
      const timer = setTimeout(stop, o.timeoutMs);
      signal?.addEventListener("abort", stop, { once: true });
      child.on("error", () => { failure = new LoopXFailure("LoopX process failed", "unknown"); });
      for (const stream of [child.stdout, child.stderr]) {
        stream.setEncoding("utf8");
        stream.on("data", (chunk: string) => {
          bytes += Buffer.byteLength(chunk);
          if (bytes > (o.maxOutputBytes ?? 4_194_304)) { stop(); return; }
          if (stream === child.stdout) stdout += chunk;
        });
      }
      child.once("close", (code) => {
        clearTimeout(timer);
        signal?.removeEventListener("abort", stop);
        if (failure) { reject(failure); return; }
        try {
          const value = object(JSON.parse(stdout), "LoopX response");
          if (!observeRejection && (code !== 0 || value.ok !== true)) throw new LoopXFailure("LoopX rejected command; inspect observation", "rejected", value);
          resolve(value);
        } catch (error) {
          reject(error instanceof LoopXFailure ? error : new LoopXFailure("LoopX response unavailable", "unknown"));
        }
      });
    });
  }
}
