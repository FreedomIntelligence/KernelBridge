import { createHash } from "node:crypto";
import { constants } from "node:fs";
import { open } from "node:fs/promises";
import { isAbsolute, posix } from "node:path";
import type { ProgramBundle, RunLaunchManifest } from "../generated/runtime.ts";
import type { TaskContractSource } from "../generated/task-contract.ts";
import { runtimeValue } from "../clients/runtime.ts";
import { canonicalKey } from "../run/scoring/canonical.ts";
import { loadTaskContract } from "./contract.ts";
import { SCHEMA as TASK_SCHEMA } from "../generated/task-contract.ts";
import { wireValidator } from "../clients/wire-schemas.ts";

export const MAX_LAUNCH_BYTES = 16 * 1024 * 1024;
export const MAX_CONTROL_BYTES = 64 * 1024;
export const MAX_STOP_REQUEST_BYTES = 16 * 1024 * 1024;
const objectText = wireValidator<string>(TASK_SCHEMA.$id, "Text");

export function digestBytes(bytes: string | Uint8Array): string {
  return `sha256:${createHash("sha256").update(bytes).digest("hex")}`;
}

export async function readBoundedUtf8(path: string, label: string, limit: number): Promise<string> {
  if (!isAbsolute(path)) throw new Error(`${label} must be an absolute path`);
  // Reject FIFOs without waiting for a writer. Inspect the opened descriptor so
  // swapping the path cannot bypass the regular-file check.
  const handle = await open(path, constants.O_RDONLY | constants.O_NONBLOCK);
  try {
    if (!(await handle.stat()).isFile()) throw new Error(`${label} must be a regular file`);
    const bytes = Buffer.alloc(limit + 1);
    let length = 0;
    while (length < bytes.length) {
      const read = await handle.read(bytes, length, bytes.length - length, null);
      if (read.bytesRead === 0) break;
      length += read.bytesRead;
    }
    if (length > limit) throw new Error(`${label} exceeds ${limit} UTF-8 bytes`);
    return new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(bytes.subarray(0, length));
  } finally {
    await handle.close();
  }
}

function scalarText(text: string, label: string): void {
  if (/[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/u.test(text)) {
    throw new Error(`${label} contains an unpaired Unicode surrogate`);
  }
}

function programPath(path: string): string {
  scalarText(path, "Program path");
  const normalized = posix.normalize(path).replace(/\/+$/u, "");
  if (path.includes("\\") || path.includes("\0") || posix.isAbsolute(path) ||
      normalized === "" || normalized === "." || normalized === ".." || normalized.startsWith("../")) {
    throw new Error(`Program path must stay within the workspace: ${path}`);
  }
  return normalized;
}

export function canonicalProgram(value: unknown): { readonly bundle: ProgramBundle; readonly bytes: string; readonly digest: string } {
  const program = runtimeValue("ProgramBundle", value);
  scalarText(program.configuration_source, "Program configuration");
  const seen = new Set<string>();
  const files = program.files.map((file) => {
    const path = programPath(file.path);
    if (seen.has(path)) throw new Error(`Duplicate normalized Program path: ${path}`);
    seen.add(path);
    scalarText(file.text, `Program file ${path}`);
    return { path, text: file.text };
  }).sort((left, right) => Buffer.compare(Buffer.from(left.path), Buffer.from(right.path)));
  const entrypoint = programPath(program.entrypoint);
  if (!seen.has(entrypoint)) throw new Error("Program entrypoint must name one of its files");
  const bundle = { ...program, entrypoint, files };
  const bytes = canonicalKey(bundle);
  if (Buffer.byteLength(bytes) > 8 * 1024 * 1024) throw new Error("Canonical Program exceeds 8 MiB");
  return { bundle, bytes, digest: digestBytes(bytes) };
}

export interface LoadedManifest {
  readonly manifest: RunLaunchManifest;
  readonly digest: string;
  readonly source: TaskContractSource;
}

export async function loadLaunchManifest(path: string): Promise<LoadedManifest> {
  const text = await readBoundedUtf8(path, "Run manifest", MAX_LAUNCH_BYTES);
  const manifest = runtimeValue("RunLaunchManifest", JSON.parse(text));
  scalarText(manifest.goal, "Run goal");
  for (const field of [manifest.proposal.conditions, manifest.proposal.operation,
    manifest.proposal.expected_outcome, ...manifest.proposal.stop_conditions]) {
    if (!objectText(field)) throw new Error("Initial proposal text cannot fit the generated domain record contract");
  }
  scalarText(manifest.contract_source_text, "TaskContract source");
  scalarText(manifest.proposal.parameter_source, "Proposal parameter source");
  const source = loadTaskContract(Buffer.from(manifest.contract_source_text));
  const program = canonicalProgram(manifest.program);
  if (source.contract.baseline.artifact_digest !== program.digest) {
    throw new Error("Manifest Program bytes differ from the TaskContract baseline digest");
  }
  if (manifest.trial_credit_limit > manifest.run_credit_limit) {
    throw new Error("Trial credit limit exceeds Run credit limit");
  }
  return { manifest, digest: digestBytes(Buffer.from(text, "utf8")), source };
}
