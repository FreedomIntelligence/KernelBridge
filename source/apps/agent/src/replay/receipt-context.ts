import { createHash } from "node:crypto";
import type { AgentMessage } from "@earendil-works/pi-agent-core";
import type { ToolResultMessage } from "@earendil-works/pi-ai";
import type { SessionEntry } from "@earendil-works/pi-coding-agent";
import { ReplayContextConflict, ReplayContextOverflow } from "./context-contract.ts";
import type { ReplayResultMetadata } from "./context-contract.ts";

export const RECEIPT_CONTEXT_ENTRY = "kernelcat.receipt-context/v1";
const MAX_BRANCH_ENTRIES = 16384;
const MAX_RECEIPTS = 4096;
const MAX_CONTENT_BYTES = 16 * 1024 * 1024;
type ReceiptStatus = ReplayResultMetadata["status"];
type ResultContent = Pick<ToolResultMessage, "content"> & { readonly isError?: boolean };

export interface ReceiptObservation {
  readonly toolCallId: string;
  readonly toolName: string;
  readonly result: ResultContent;
  /** Actual finite RPC reply, supplied only by the trusted adapter. */
  readonly response: unknown;
}

interface ReceiptRecord {
  readonly toolCallId: string;
  readonly toolName: string;
  readonly actionId: string;
  readonly status: ReceiptStatus;
  readonly contentDigest: string;
}

export interface ReceiptContextBinding {
  /** Extension-owned native persistence; entries must be the selected branch. */
  bind(append: (type: string, data: unknown) => void, entries: readonly SessionEntry[]): void;
  observeDirect(observation: ReceiptObservation): void;
  /** Undefined nested replies means cached/unproven, never successful by inference. */
  observeNative(observation: ReceiptObservation, nestedReplies?: readonly unknown[]): void;
  metadata(messages: readonly AgentMessage[], entries: readonly SessionEntry[]): readonly ReplayResultMetadata[];
}

function object(value: unknown, description: string): Record<string, unknown> {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`Invalid ${description}`);
  }
  return value as Record<string, unknown>;
}

function identity(value: unknown, description: string): string {
  if (typeof value !== "string" || !value.length || value.length > 512 || /[\s\u0000-\u001f\u007f]/u.test(value)) {
    throw new Error(`Invalid receipt context ${description}`);
  }
  return value;
}

function status(value: unknown): ReceiptStatus {
  if (value !== "completed" && value !== "failed" && value !== "unknown") {
    throw new Error("Invalid receipt context outcome status");
  }
  return value;
}

function receipt(response: unknown): { actionId: string; status: ReceiptStatus } {
  const reply = object(response, "receipt context RPC reply");
  const outcome = object(reply.outcome, "receipt context RPC outcome");
  return { actionId: identity(reply.action_id, "action identity"), status: status(outcome.status) };
}

/** Digest of JSON.stringify(content) only: not a Gateway receipt or performance digest. */
function contentDigest(content: ResultContent["content"]): string {
  const encoded = JSON.stringify(content);
  if (Buffer.byteLength(encoded) > MAX_CONTENT_BYTES) {
    throw new ReplayContextOverflow("Receipt result content exceeds 16 MiB");
  }
  return `sha256:${createHash("sha256").update(encoded).digest("hex")}`;
}

function checkedRecord(value: unknown): ReceiptRecord {
  const data = object(value, "native receipt context entry");
  const keys = Object.keys(data).sort().join(",");
  if (keys !== "actionId,contentDigest,status,toolCallId,toolName"
      || typeof data.contentDigest !== "string" || !/^sha256:[a-f0-9]{64}$/u.test(data.contentDigest)) {
    throw new Error("Invalid native receipt context entry fields");
  }
  return Object.freeze({ toolCallId: identity(data.toolCallId, "tool call identity"),
    toolName: identity(data.toolName, "tool name"), actionId: identity(data.actionId, "action identity"),
    status: status(data.status), contentDigest: data.contentDigest });
}

function addRecord(records: Map<string, ReceiptRecord>, record: ReceiptRecord): boolean {
  const previous = records.get(record.toolCallId);
  if (previous !== undefined) {
    if (JSON.stringify(previous) !== JSON.stringify(record)) {
      throw new ReplayContextConflict(`Conflicting receipt context identity ${record.toolCallId}`);
    }
    return false;
  }
  if (records.size >= MAX_RECEIPTS) throw new ReplayContextOverflow("Receipt context exceeds 4096 identities");
  records.set(record.toolCallId, record);
  return true;
}

function restore(entries: readonly SessionEntry[]): Map<string, ReceiptRecord> {
  if (entries.length > MAX_BRANCH_ENTRIES) throw new ReplayContextOverflow("Receipt context branch exceeds 16384 entries");
  const records = new Map<string, ReceiptRecord>();
  for (const entry of entries) {
    if (entry.type === "custom" && entry.customType === RECEIPT_CONTEXT_ENTRY) {
      addRecord(records, checkedRecord(entry.data));
    }
  }
  return records;
}

function nativeStatus(observation: ReceiptObservation, nestedReplies?: readonly unknown[]): ReceiptStatus {
  const outer = receipt(observation.response);
  if (nestedReplies === undefined) return outer.status === "failed" ? "failed" : "unknown";
  if (nestedReplies.length > 64) throw new ReplayContextOverflow("Native receipt context exceeds 64 nested calls");
  let selected = outer.status;
  for (let index = 0; index < nestedReplies.length; index += 1) {
    const nested = receipt(nestedReplies[index]);
    if (nested.actionId !== `${outer.actionId}/nested/${index}`) {
      throw new ReplayContextConflict(`Conflicting nested receipt identity ${nested.actionId}`);
    }
    if (nested.status === "unknown" || selected === "unknown") selected = "unknown";
    else if (nested.status === "failed") selected = "failed";
  }
  return selected;
}

/** Ephemeral index of original Pi custom entries, never another durable receipt store. */
export function createReceiptContextBinding(): ReceiptContextBinding {
  let records = new Map<string, ReceiptRecord>();
  let append: ((type: string, data: unknown) => void) | undefined;

  function observe(observation: ReceiptObservation, outcome: ReceiptStatus): void {
    if (append === undefined) throw new Error("Receipt context requires native session_start before observation");
    const original = receipt(observation.response);
    const record = checkedRecord({ toolCallId: observation.toolCallId, toolName: observation.toolName,
      actionId: original.actionId, status: outcome, contentDigest: contentDigest(observation.result.content) });
    if (!addRecord(records, record)) return;
    append(RECEIPT_CONTEXT_ENTRY, record);
  }

  return {
    bind(writer, entries) {
      records = restore(entries);
      append = writer;
    },
    observeDirect(observation) {
      observe(observation, receipt(observation.response).status);
    },
    observeNative(observation, nestedReplies) {
      observe(observation, nativeStatus(observation, nestedReplies));
    },
    metadata(messages, entries) {
      records = restore(entries);
      if (messages.length > MAX_RECEIPTS) throw new ReplayContextOverflow("Receipt context exceeds 4096 messages");
      return messages.flatMap((message, index) => {
        if (message.role !== "toolResult") return [];
        const record = records.get(message.toolCallId);
        if (record === undefined) return [];
        if (record.toolName !== message.toolName || record.contentDigest !== contentDigest(message.content)) {
          throw new ReplayContextConflict(`Receipt context differs from original result ${message.toolCallId}`);
        }
        return [{ index, status: record.status, reference: {
          uri: `read-receipt:${encodeURIComponent(record.actionId)}`, digest: record.contentDigest,
        } }];
      });
    },
  };
}
