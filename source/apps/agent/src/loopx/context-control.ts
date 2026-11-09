import { writeFile, rename } from "node:fs/promises";
import { join } from "node:path";
import type { EffectRpc } from "../clients/rpc.ts";
import type { ReplayContextBoundary, ReplayContextManifest } from "../generated/harness.ts";
import type { JsonObject } from "../generated/context.ts";
import { contextContract, contextDigest, contextMessages, MAX_CONTEXT_BYTES } from "../replay/context-contract.ts";
import { DEFAULT_REPLAY_CLEANUP_OPTIONS, projectReplayContext } from "../replay/context-cleanup.ts";
import type { ReplayCleanupOptions } from "../replay/context-cleanup.ts";

/** Ephemeral control file, not an execution ledger. Only the outer host chooses edits. */
export function controlledContext(rpc: EffectRpc, directory: string,
  options: ReplayCleanupOptions = DEFAULT_REPLAY_CLEANUP_OPTIONS): EffectRpc {
  let revision = 0;
  return {
    async call(method, params = {}) {
      if (method !== "context_boundary") return await rpc.call(method, params);
      if (Buffer.byteLength(JSON.stringify(params)) > MAX_CONTEXT_BYTES) throw new Error("Context boundary exceeds 16 MiB");
      const request = contextContract<ReplayContextBoundary>(params, "ReplayContextBoundary");
      const projection = projectReplayContext(contextMessages(request.messages), options, request.metadata);
      const manifest = contextContract<ReplayContextManifest>({ schema_version: "kernelcat.context-manifest/v1",
        revision: revision + 1, prefix_count: request.messages.length,
        messages: projection.messages, report: projection.report }, "ReplayContextManifest");
      const encoded = JSON.stringify(manifest);
      if (Buffer.byteLength(encoded) > MAX_CONTEXT_BYTES) throw new Error("Context manifest exceeds 16 MiB");
      const path = join(directory, "context-manifest.json");
      const pending = join(directory, "context-manifest.pending");
      await writeFile(pending, encoded, { flag: "wx", mode: 0o600 });
      await rename(pending, path);
      revision = manifest.revision;
      return { path, revision, digest: contextDigest(manifest) } satisfies JsonObject;
    },
  };
}
