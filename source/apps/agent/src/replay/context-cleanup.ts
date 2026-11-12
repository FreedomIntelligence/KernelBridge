import { createHash } from "node:crypto";
import type { AgentMessage } from "@earendil-works/pi-agent-core";
import type { ToolResultMessage } from "@earendil-works/pi-ai";

import { REPLAY_CONTEXT_RULE, ReplayContextConflict, ReplayContextOverflow, immutableReplayReferenceText } from "./context-contract.ts";
import type { ReplayCleanupDecision, ReplayCleanupReport, ReplayResultMetadata } from "./context-contract.ts";
export { REPLAY_CONTEXT_RULE, ReplayContextConflict, ReplayContextOverflow, immutableReplayReferenceText } from "./context-contract.ts";
export type { ReplayCleanupDecision, ReplayCleanupReport, ReplayResultMetadata } from "./context-contract.ts";

export interface ReplayCleanupOptions {
  readonly keepRecentModelTurns: number;
  readonly longResultBytes: number;
  readonly maxMessages: number;
  readonly maxInputBytes: number;
  readonly maxOutputBytes: number;
}

export const DEFAULT_REPLAY_CLEANUP_OPTIONS: ReplayCleanupOptions = Object.freeze({
  keepRecentModelTurns: 2,
  longResultBytes: 8192,
  maxMessages: 4096,
  maxInputBytes: 16 * 1024 * 1024,
  maxOutputBytes: 16 * 1024 * 1024,
});

/** Actual owner-composed request bytes, never an estimate from the model token window. */
export interface ReplayCleanupBudget {
  readonly inputBytes: number;
  readonly targetBytes: number;
}

type DecisionReason = ReplayCleanupDecision["reason"];

export interface ReplayCleanupProjection {
  readonly messages: AgentMessage[];
  readonly report: ReplayCleanupReport;
}

function validateOptions(options: ReplayCleanupOptions): void {
  const maximums: ReplayCleanupOptions = {
    keepRecentModelTurns: 4096, longResultBytes: 64 * 1024 * 1024,
    maxMessages: 4096, maxInputBytes: 64 * 1024 * 1024, maxOutputBytes: 64 * 1024 * 1024,
  };
  for (const key of Object.keys(maximums) as (keyof ReplayCleanupOptions)[]) {
    if (!Number.isSafeInteger(options[key]) || options[key] < 1 || options[key] > maximums[key]) {
      throw new Error(`Replay context option ${key} must be an integer in [1, ${maximums[key]}]`);
    }
  }
}

function textBlocks(message: AgentMessage): Set<string> {
  if (!("content" in message)) return new Set();
  if (typeof message.content === "string") return new Set([message.content]);
  return new Set(message.content.filter((block) => block.type === "text").map((block) => block.text));
}

function checkedMetadata(messages: readonly AgentMessage[], values: readonly ReplayResultMetadata[]): {
  byIndex: Map<number, ReplayResultMetadata>;
  anchors: Set<number>;
} {
  if (values.length > messages.length) throw new Error("Too many replay result metadata items");
  const byIndex = new Map<number, ReplayResultMetadata>();
  const anchors = new Set<number>();
  const anchorTexts = new Map<number, Set<string>>();
  for (const value of values) {
    if (!Number.isSafeInteger(value.index) || messages[value.index]?.role !== "toolResult"
        || byIndex.has(value.index) || !["completed", "failed", "unknown"].includes(value.status)) {
      throw new Error(`Invalid replay result metadata at index ${value.index}`);
    }
    const reference = value.reference;
    if (reference !== undefined) {
      if (typeof reference.uri !== "string" || reference.uri.length > 2048
          || !/^[A-Za-z][A-Za-z0-9+.-]*:[^\s\u0000-\u001f\u007f]{1,2048}$/.test(reference.uri)
          || !/^sha256:[a-f0-9]{64}$/.test(reference.digest)) {
        throw new Error(`Invalid immutable replay reference at index ${value.index}`);
      }
      if (reference.contextIndex !== undefined) {
        const anchor = messages[reference.contextIndex];
        if (!Number.isSafeInteger(reference.contextIndex) || !anchor || reference.contextIndex === value.index) {
          throw new Error(`Invalid immutable replay reference at index ${value.index}`);
        }
        if (!anchorTexts.has(reference.contextIndex)) {
          anchorTexts.set(reference.contextIndex, textBlocks(anchor));
        }
        if (!anchorTexts.get(reference.contextIndex)!.has(immutableReplayReferenceText(reference))) {
          throw new Error(`Replay reference is absent from retained anchor ${reference.contextIndex}`);
        }
        anchors.add(reference.contextIndex);
      }
    }
    byIndex.set(value.index, value);
  }
  return { byIndex, anchors };
}

function encode(messages: readonly AgentMessage[], limit: number): { strings: string[]; bytes: number; digest: string } {
  const strings: string[] = [];
  const hash = createHash("sha256").update("[");
  let bytes = 2;
  if (bytes > limit) throw new ReplayContextOverflow(`Replay context exceeds ${limit} bytes`);
  for (const message of messages) {
    const encoded = JSON.stringify(message);
    const prefix = strings.length === 0 ? "" : ",";
    bytes += Buffer.byteLength(encoded) + prefix.length;
    if (bytes > limit) throw new ReplayContextOverflow(`Replay context exceeds ${limit} bytes`);
    hash.update(prefix).update(encoded);
    strings.push(encoded);
  }
  return { strings, bytes, digest: `sha256:${hash.update("]").digest("hex")}` };
}

function recentBoundary(messages: readonly AgentMessage[], keep: number): number {
  let turns = 0;
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    if (messages[index].role === "assistant" && ++turns === keep) return index;
  }
  return 0;
}

interface Exchange {
  readonly start: number;
  readonly results: readonly number[];
  readonly complete: boolean;
}

function checkResultIdentities(messages: readonly AgentMessage[], strings: readonly string[]): void {
  const knownResults = new Map<string, string>();
  messages.forEach((message, index) => {
    if (message.role !== "toolResult") return;
    if (knownResults.has(message.toolCallId) && knownResults.get(message.toolCallId) !== strings[index]) {
      throw new ReplayContextConflict(`Conflicting replay result ${message.toolCallId}`);
    }
    knownResults.set(message.toolCallId, strings[index]);
  });
}

/** A call identity is checked globally; only redundant results in one exchange are removable. */
function exchanges(messages: readonly AgentMessage[], strings: readonly string[]): Exchange[] {
  checkResultIdentities(messages, strings);
  const knownCalls = new Map<string, string>();
  const groups: Exchange[] = [];
  for (let index = 0; index < messages.length; index += 1) {
    const message = messages[index];
    if (message.role !== "assistant") continue;
    const calls = new Map<string, string>();
    for (const block of message.content) {
      if (block.type !== "toolCall") continue;
      const encoded = JSON.stringify(block);
      if (calls.has(block.id) || (knownCalls.has(block.id) && knownCalls.get(block.id) !== encoded)) {
        throw new ReplayContextConflict(`Conflicting replay tool call ${block.id}`);
      }
      knownCalls.set(block.id, encoded);
      calls.set(block.id, block.name);
    }
    if (calls.size === 0) continue;
    const start = index;
    const results: number[] = [];
    const received = new Set<string>();
    while (messages[index + 1]?.role === "toolResult") {
      index += 1;
      const result = messages[index] as ToolResultMessage;
      if (calls.get(result.toolCallId) !== result.toolName) break;
      received.add(result.toolCallId);
      results.push(index);
    }
    groups.push({ start, results, complete: received.size === calls.size });
  }
  return groups;
}

function reduceResult(message: ToolResultMessage, metadata: ReplayResultMetadata | undefined,
  options: ReplayCleanupOptions): { message: ToolResultMessage; reason: DecisionReason } {
  if (metadata?.reference === undefined) return { message, reason: "no_immutable_reference" };
  if (Buffer.byteLength(JSON.stringify(message.content)) < options.longResultBytes) {
    return { message, reason: "short_result" };
  }
  const content = [{ type: "text" as const, text: immutableReplayReferenceText(metadata.reference) }];
  if (Buffer.byteLength(JSON.stringify(content)) >= Buffer.byteLength(JSON.stringify(message.content))) {
    return { message, reason: "short_result" };
  }
  return { message: { ...message, content }, reason: "immutable_reference" };
}

function protectedExchange(group: Exchange, messages: readonly AgentMessage[], recent: number,
  metadata: ReadonlyMap<number, ReplayResultMetadata>): DecisionReason | null {
  if (!group.complete) return "unpaired_or_pending";
  if (group.start >= recent) return "recent";
  const unsuccessful = group.results.some((index) => metadata.get(index)?.status !== "completed"
    || (messages[index] as ToolResultMessage).isError);
  return unsuccessful ? "unproven_or_unsuccessful" : null;
}

/** Pure request projection. Inputs and append-only session history are never mutated. */
export function projectReplayContext(messages: readonly AgentMessage[], options: ReplayCleanupOptions,
  metadata: readonly ReplayResultMetadata[] = [], budget?: ReplayCleanupBudget): ReplayCleanupProjection {
  validateOptions(options);
  if (messages.length > options.maxMessages) throw new ReplayContextOverflow("Replay context message limit exceeded");
  const original = encode(messages, options.maxInputBytes);
  let remainingBytes = budget?.inputBytes ?? original.bytes;
  const targetBytes = budget?.targetBytes ?? options.maxOutputBytes;
  if (!Number.isSafeInteger(remainingBytes) || remainingBytes < 0
      || !Number.isSafeInteger(targetBytes) || targetBytes < 1) {
    throw new Error("Invalid trusted replay request byte budget");
  }
  const { byIndex, anchors } = checkedMetadata(messages, metadata);
  const recent = recentBoundary(messages, options.keepRecentModelTurns);
  const projected = new Map<number, AgentMessage | null>();
  const decisions: ReplayCleanupDecision[] = messages.map((message, index) => ({ index, action: "keep",
    reason: message.role === "user" ? "user" : message.role === "toolResult" ? "unpaired_or_pending" : "non_result" }));
  for (const group of exchanges(messages, original.strings)) {
    const protectedReason = protectedExchange(group, messages, recent, byIndex);
    const delivered = new Set<string>();
    for (const index of group.results) {
      const message = messages[index] as ToolResultMessage;
      const reason = anchors.has(index) ? "reference_anchor" : protectedReason;
      if (reason !== null) {
        decisions[index] = { index, action: "keep", reason };
      } else if (delivered.has(message.toolCallId)) {
        projected.set(index, null);
        remainingBytes -= Buffer.byteLength(original.strings[index]) + 1;
        decisions[index] = { index, action: "remove", reason: "duplicate_delivery" };
      } else if (remainingBytes <= targetBytes) {
        decisions[index] = { index, action: "keep", reason: "within_budget" };
      } else {
        const reduced = reduceResult(message, byIndex.get(index), options);
        remainingBytes -= Buffer.byteLength(original.strings[index]) - Buffer.byteLength(JSON.stringify(reduced.message));
        projected.set(index, reduced.message);
        decisions[index] = { index, action: reduced.message === message ? "keep" : "replace", reason: reduced.reason };
      }
      delivered.add(message.toolCallId);
    }
  }
  const output = messages.flatMap((message, index) => {
    const selected = projected.has(index) ? projected.get(index) : message;
    return selected ? [selected] : [];
  });
  const emitted = encode(output, options.maxOutputBytes);
  return { messages: output, report: { rule: REPLAY_CONTEXT_RULE, inputDigest: original.digest,
    outputDigest: emitted.digest, inputBytes: original.bytes, outputBytes: emitted.bytes,
    changed: decisions.some((decision) => decision.action !== "keep"), decisions } };
}
