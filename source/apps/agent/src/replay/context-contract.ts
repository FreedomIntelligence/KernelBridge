import { createHash } from "node:crypto";
import type { AgentMessage } from "@earendil-works/pi-agent-core";
import { SCHEMA } from "../generated/harness.ts";
import type { JsonObject } from "../generated/context.ts";
import { wireValidator } from "../clients/wire-schemas.ts";
import { validatedSnapshot } from "../context/json-snapshot.ts";

export type { ReplayCleanupDecision, ReplayCleanupReport, ReplayResultMetadata } from "../generated/harness.ts";
export const REPLAY_CONTEXT_RULE = "kernelcat.replay-context/v2";
export const MAX_CONTEXT_BYTES = 16 * 1024 * 1024;
export class ReplayContextConflict extends Error {}
export class ReplayContextOverflow extends Error {}

export function contextDigest(value: unknown): string {
  return `sha256:${createHash("sha256").update(JSON.stringify(value)).digest("hex")}`;
}

export function contextContract<T>(value: unknown, definition: string): T {
  return validatedSnapshot(value, wireValidator<T>(SCHEMA.$id, definition), definition);
}

/** Validate the SDK message fields consumed by projection, without interpreting message text. */
export function contextMessages(values: readonly JsonObject[]): AgentMessage[] {
  for (const value of values) {
    if (typeof value.role !== "string") throw new Error("Context message requires a role");
    if (value.role === "assistant" && !Array.isArray(value.content)) throw new Error("Invalid assistant content");
    if (value.role === "toolResult" && (typeof value.toolCallId !== "string"
        || typeof value.toolName !== "string" || !Array.isArray(value.content))) {
      throw new Error("Invalid context tool result");
    }
    if (Array.isArray(value.content)) {
      for (const block of value.content) {
        if (block === null || typeof block !== "object" || Array.isArray(block) || typeof block.type !== "string") {
          throw new Error("Invalid context content block");
        }
        if (block.type === "toolCall" && (typeof block.id !== "string" || typeof block.name !== "string")) {
          throw new Error("Invalid context tool call");
        }
      }
    }
  }
  return values as unknown as AgentMessage[];
}

export function immutableReplayReferenceText(reference: { readonly uri: string; readonly digest: string }): string {
  if (reference.uri.startsWith("read-receipt:")) {
    const arguments_ = JSON.stringify({ action_id: decodeURIComponent(reference.uri.slice("read-receipt:".length)) });
    return `Original successful output: read_receipt(${arguments_}) (content ${reference.digest}).`;
  }
  return `Original successful output: ${reference.uri} (${reference.digest}).`;
}
