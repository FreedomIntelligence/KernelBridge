import { openSync, readFileSync, fstatSync, closeSync, constants } from "node:fs";
import { isAbsolute } from "node:path";
import type { AgentMessage } from "@earendil-works/pi-agent-core";
import type { ReplayContextManifest, ReplayContextManifestRef } from "../generated/harness.ts";
import { contextContract, contextDigest, contextMessages, MAX_CONTEXT_BYTES, ReplayContextConflict } from "./context-contract.ts";

function readManifest(reference: ReplayContextManifestRef): ReplayContextManifest {
  if (!isAbsolute(reference.path)) throw new Error("Context manifest path must be absolute");
  const file = openSync(reference.path, constants.O_RDONLY | constants.O_NOFOLLOW);
  try {
    const stat = fstatSync(file);
    if (!stat.isFile() || stat.size > MAX_CONTEXT_BYTES) throw new Error("Invalid context manifest file");
    const raw: unknown = JSON.parse(readFileSync(file, "utf8"));
    if (contextDigest(raw) !== reference.digest) throw new ReplayContextConflict("Context manifest digest changed");
    const manifest = contextContract<ReplayContextManifest>(raw, "ReplayContextManifest");
    if (manifest.revision !== reference.revision) throw new ReplayContextConflict("Context manifest revision changed");
    return manifest;
  } finally {
    closeSync(file);
  }
}

/** Synchronous application of an admitted prefix. Newly appended messages stay byte-for-byte intact. */
export function applyContextManifest(reference: ReplayContextManifestRef, messages: readonly AgentMessage[]): {
  messages: AgentMessage[]; manifest: ReplayContextManifest;
} {
  const manifest = readManifest(reference);
  const prefix = messages.slice(0, manifest.prefix_count);
  if (prefix.length !== manifest.prefix_count || contextDigest(prefix) !== manifest.report.inputDigest) {
    throw new ReplayContextConflict("Context manifest does not match the current session prefix");
  }
  if (contextDigest(manifest.messages) !== manifest.report.outputDigest) throw new Error("Invalid context manifest output");
  if (manifest.report.decisions.length !== prefix.length) throw new Error("Incomplete context manifest decisions");
  const projected = contextMessages(manifest.messages);
  let outputIndex = 0;
  const output = manifest.report.decisions.flatMap((decision, index) => {
    if (decision.index !== index) throw new Error("Context manifest decisions are not contiguous");
    if (decision.action === "remove") return [];
    const replacement = projected[outputIndex++];
    if (!replacement) throw new Error("Context manifest projection is incomplete");
    return [decision.action === "keep" ? prefix[index] : { ...prefix[index], ...replacement } as AgentMessage];
  });
  if (outputIndex !== projected.length || contextDigest(output) !== manifest.report.outputDigest) {
    throw new Error("Context manifest decisions do not match its projection");
  }
  return { messages: [...output, ...messages.slice(manifest.prefix_count)], manifest };
}
