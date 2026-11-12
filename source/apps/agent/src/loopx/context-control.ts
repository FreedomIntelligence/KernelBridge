import { writeFile, rename } from "node:fs/promises";
import { join } from "node:path";
import { convertToLlm } from "@earendil-works/pi-coding-agent";
import type { EffectRpc } from "../clients/rpc.ts";
import type { HarnessInput, ReplayContextBoundary, ReplayContextManifest, ReplayResultMetadata } from "../generated/harness.ts";
import type { ContextSource, JsonObject } from "../generated/context.ts";
import { contextContract, contextDigest, contextMessages, MAX_CONTEXT_BYTES, ReplayContextConflict, ReplayContextOverflow } from "../replay/context-contract.ts";
import { DEFAULT_REPLAY_CLEANUP_OPTIONS, projectReplayContext } from "../replay/context-cleanup.ts";
import type { ReplayCleanupOptions } from "../replay/context-cleanup.ts";
import { encodedBytes } from "../context/compiler.ts";
import { harnessMessage } from "../harness/pi/input.ts";
import { canonicalKey } from "../run/scoring/canonical.ts";

/** Keep a measured margin for next-input/envelope growth; this is not an output-token allowance. */
export function retentionReserve(limit: number): number { return Math.min(65536, Math.floor(limit / 16)); }

/** Ephemeral original-boundary index. Receipts and request bytes remain in their existing owners. */
export function createContextControl(directory: string, options: ReplayCleanupOptions = DEFAULT_REPLAY_CLEANUP_OPTIONS) {
  let revision = 0;
  let boundary: { messages: readonly JsonObject[]; metadata: readonly ReplayResultMetadata[] } | undefined;

  async function prepare(params: Readonly<Record<string, unknown>>): Promise<JsonObject> {
    if (Buffer.byteLength(JSON.stringify(params)) > MAX_CONTEXT_BYTES) throw new Error("Context boundary exceeds 16 MiB");
    const request = contextContract<ReplayContextBoundary>(params, "ReplayContextBoundary");
    // No owner-composed request exists here yet. Validate identities and remove only
    // redundant deliveries; age alone must not discard successful reference content.
    const projection = projectReplayContext(contextMessages(request.messages),
      { ...options, maxOutputBytes: options.maxInputBytes }, request.metadata);
    const manifest = contextContract<ReplayContextManifest>({ schema_version: "kernelcat.context-manifest/v1",
      revision: revision + 1, prefix_count: request.messages.length,
      messages: projection.messages, report: projection.report }, "ReplayContextManifest");
    const encoded = JSON.stringify(manifest);
    if (Buffer.byteLength(encoded) > MAX_CONTEXT_BYTES) throw new Error("Context manifest exceeds 16 MiB");
    const path = join(directory, "context-manifest.json");
    const pending = join(directory, "context-manifest.pending");
    await writeFile(pending, encoded, { flag: "wx", mode: 0o600 });
    await rename(pending, path);
    // The boundary contains AgentMessage, not only provider Message. Use the
    // pinned SDK conversion for !bash, custom, branch and compaction summaries;
    // !!bash is omitted exactly as it is by the actual Pi model transport.
    const indexes = new Map<number, readonly number[]>();
    const messages: JsonObject[] = [];
    let projectedIndex = 0;
    for (const decision of projection.report.decisions) {
      if (decision.action === "remove") continue;
      const original = projection.messages[projectedIndex++];
      const converted = convertToLlm([original]).filter(message => message.role !== "system").map(harnessMessage);
      indexes.set(decision.index, converted.map((_, offset) => messages.length + offset));
      messages.push(...converted);
    }
    function retainedIndex(index: number): number {
      const selected = indexes.get(index);
      if (!selected || selected.length !== 1) {
        throw new ReplayContextConflict("Receipt metadata has no unique model-visible original message");
      }
      return selected[0];
    }
    boundary = { messages,
      metadata: request.metadata.flatMap(item => indexes.has(item.index) ? [{ ...item, index: retainedIndex(item.index),
        ...(item.reference?.contextIndex === undefined ? {} : { reference: { ...item.reference,
          contextIndex: retainedIndex(item.reference.contextIndex) } }) }] : []),
    };
    revision = manifest.revision;
    return { path, revision, digest: contextDigest(manifest) };
  }

  function project(harness: HarnessInput, source: ContextSource): HarnessInput {
    if (!boundary) throw new ReplayContextConflict("Model input has no trusted replay boundary");
    const prefix = harness.messages.slice(0, boundary.messages.length);
    if (canonicalKey(prefix) !== canonicalKey(boundary.messages)) {
      throw new ReplayContextConflict("Model input differs from its trusted original replay prefix");
    }
    const limit = source.policy.context_bytes;
    if (typeof limit !== "number" || !Number.isSafeInteger(limit) || limit < 1
        || limit !== source.projection.context_byte_limit) {
      throw new ReplayContextConflict("Model input lacks an authoritative context byte limit");
    }
    const projection = projectReplayContext(contextMessages(harness.messages), options, boundary.metadata,
      { inputBytes: encodedBytes(harness), targetBytes: Math.max(1, limit - retentionReserve(limit)) });
    const selected = contextContract<HarnessInput>({ ...harness, messages: projection.messages }, "HarnessInput");
    // A safety margin is best effort; mandatory/unknown/recent exchanges never
    // become fabricated successes to fit. The original hard limit still applies.
    if (encodedBytes(selected) > limit) throw new ReplayContextOverflow("Protected model input exceeds context_bytes");
    return selected;
  }

  return {
    project,
    wrap(rpc: EffectRpc): EffectRpc {
      return { async call(method, params = {}) {
        return method === "context_boundary" ? prepare(params) : rpc.call(method, params);
      } };
    },
  };
}

/** Compatibility wrapper: owners that dispatch models bind createContextControl.project explicitly. */
export function controlledContext(rpc: EffectRpc, directory: string,
  options: ReplayCleanupOptions = DEFAULT_REPLAY_CLEANUP_OPTIONS): EffectRpc {
  return createContextControl(directory, options).wrap(rpc);
}
