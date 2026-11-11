import type { AgentMessage } from "@earendil-works/pi-agent-core";
import { buildSessionContext } from "@earendil-works/pi-coding-agent";
import type {
  CompactionEntry, CompactionResult, ExtensionAPI, ExtensionContext, ExtensionFactory, SessionBeforeCompactEvent,
  SessionEntry, SessionStartEvent,
} from "@earendil-works/pi-coding-agent";
import type { EffectRpc } from "../../clients/rpc.ts";
import type { ReplayContextManifestRef } from "../../generated/harness.ts";
import type { ReplayResultMetadata } from "../../replay/context-contract.ts";
import {
  contextContract, MAX_CONTEXT_BYTES, ReplayContextConflict, ReplayContextOverflow, REPLAY_CONTEXT_RULE,
} from "../../replay/context-contract.ts";
import { applyContextManifest } from "../../replay/context-manifest.ts";
import type { ReceiptContextBinding } from "../../replay/receipt-context.ts";

export interface ReplayContextBinding {
  readonly rpc: EffectRpc;
  /** Must latch the failure before the controlled provider can dispatch. */
  readonly fail: (error: unknown) => void;
  /** Trusted receipt owner only, never model text or tool details. */
  readonly metadata?: (messages: readonly AgentMessage[], context: ExtensionContext) => readonly ReplayResultMetadata[];
  readonly receipts?: ReceiptContextBinding;
  readonly replayOnCompaction?: boolean;
  readonly semanticCompaction?: boolean;
  /** A closed invocation cannot append a boundary beyond its admitted checkpoint. */
  readonly isStopped?: () => boolean;
}

export const COMPACTION_REPLAY_ENTRY = "kernelcat.replay-compaction/v1";
const MAX_BRANCH_ENTRIES = 16384;
const MAX_PRESERVED_MESSAGES = 4096;

interface CompactionObservation {
  readonly compactionEntryId: string;
  readonly status: "started" | "completed";
}

function boundedBranch(entries: SessionEntry[]): SessionEntry[] {
  if (entries.length > MAX_BRANCH_ENTRIES) {
    throw new ReplayContextOverflow("Replay branch exceeds 16384 entries");
  }
  return entries;
}

function latestCompaction(entries: SessionEntry[]): CompactionEntry | undefined {
  for (let index = entries.length - 1; index >= 0; index -= 1) {
    const entry = entries[index];
    if (entry.type === "compaction") {
      return entry;
    }
  }
  return undefined;
}

function observation(value: unknown): CompactionObservation {
  if (value === null || typeof value !== "object" || Array.isArray(value)
      || Object.keys(value).sort().join(",") !== "compactionEntryId,status"
      || !("compactionEntryId" in value) || typeof value.compactionEntryId !== "string"
      || !value.compactionEntryId.length || value.compactionEntryId.length > 512
      || !("status" in value) || (value.status !== "started" && value.status !== "completed")) {
    throw new Error("Invalid native compaction replay observation");
  }
  return { compactionEntryId: value.compactionEntryId, status: value.status };
}

/** Original-session markers form a bounded observation index, never an effect ledger. */
function compactionObservations(entries: SessionEntry[]): Map<string, CompactionObservation["status"]> {
  boundedBranch(entries);
  const states = new Map<string, CompactionObservation["status"]>();
  const compactions = new Set(entries.filter((entry) => entry.type === "compaction").map((entry) => entry.id));
  for (const entry of entries) {
    if (entry.type !== "custom" || entry.customType !== COMPACTION_REPLAY_ENTRY) {
      continue;
    }
    const marker = observation(entry.data);
    const previous = states.get(marker.compactionEntryId);
    if (!compactions.has(marker.compactionEntryId) || (marker.status === "started" ? previous !== undefined : previous !== "started")) {
      throw new ReplayContextConflict(`Invalid compaction replay transition ${marker.compactionEntryId}`);
    }
    states.set(marker.compactionEntryId, marker.status);
  }
  return states;
}

function requireCompleteExchanges(messages: readonly AgentMessage[]): void {
  let calls = new Map<string, string>();
  const pending = new Set<string>();
  for (const message of messages) {
    if (message.role === "toolResult") {
      if (calls.get(message.toolCallId) !== message.toolName) {
        throw new Error("Replay compaction refuses an unpaired tool result");
      }
      pending.delete(message.toolCallId);
      continue;
    }
    if (pending.size) {
      throw new Error("Replay compaction refuses a pending tool exchange");
    }
    calls = new Map();
    if (message.role !== "assistant") {
      continue;
    }
    for (const block of message.content) {
      if (block.type !== "toolCall") {
        continue;
      }
      if (calls.has(block.id)) {
        throw new ReplayContextConflict(`Duplicate replay tool call ${block.id}`);
      }
      calls.set(block.id, block.name);
      pending.add(block.id);
    }
  }
  if (pending.size) {
    throw new Error("Replay compaction refuses a pending tool exchange");
  }
}

function preservedEntryId(entries: SessionEntry[]): string {
  if (entries.some((entry) => entry.type === "context_edit")) {
    throw new Error("Replay compaction refuses context-edited original entries");
  }
  const messages = entries.flatMap((entry) => entry.type === "message" && entry.message.role !== "system" ? [entry.message] : []);
  if (messages.length > MAX_PRESERVED_MESSAGES || Buffer.byteLength(JSON.stringify(entries)) > MAX_CONTEXT_BYTES - 4096) {
    throw new ReplayContextOverflow("Replay compaction cannot preserve the bounded original branch");
  }
  requireCompleteExchanges(messages);
  const first = entries.find((entry) => (entry.type === "message" && entry.message.role !== "system")
    || entry.type === "custom_message" || entry.type === "branch_summary");
  if (!first) {
    throw new Error("Replay compaction requires original conversation entries");
  }
  return first.id;
}

class ContextManifestBinding {
  readonly binding: ReplayContextBinding;
  reference?: ReplayContextManifestRef;
  failure?: Error;
  private inFlight?: { readonly entryId: string; readonly completion: Promise<void> };

  constructor(binding: ReplayContextBinding) { this.binding = binding; }

  recordFailure(error: unknown, context: ExtensionContext): Error {
    this.failure ??= error instanceof Error ? error : new Error("Replay context projection failed", { cause: error });
    try { this.binding.fail(this.failure); }
    finally { context.abort(); }
    return this.failure;
  }

  fail(error: unknown, context: ExtensionContext): never {
    throw this.recordFailure(error, context);
  }

  async start(context: ExtensionContext, append: (type: string, data: unknown) => void): Promise<void> {
    try {
      const entries = boundedBranch(context.sessionManager.getBranch());
      this.binding.receipts?.bind(append, entries);
      if (this.binding.replayOnCompaction && [...compactionObservations(entries).values()].includes("started")) {
        throw new Error("Interrupted compaction replay observation requires reconciliation");
      }
      // Import verifies its admitted leaf after session_start. Preparing a fresh
      // process-local manifest must not append markers or change that identity.
      await this.prepare(buildSessionContext(entries).messages, context);
    } catch (error) {
      this.fail(error, context);
    }
  }

  async prepare(transcript: AgentMessage[], context: ExtensionContext): Promise<void> {
    try {
      if (this.failure) throw this.failure;
      const { binding } = this;
      // Locked Pi context hooks expose conversation only; Pi restores system/tool declarations.
      const messages = transcript.filter((message) => message.role !== "system");
      if (binding.receipts && binding.metadata) throw new Error("Replay context requires one trusted metadata owner");
      const metadata = binding.receipts?.metadata(messages, context.sessionManager.getBranch())
        ?? binding.metadata?.(messages, context) ?? [];
      // IPC is confined to a lifecycle boundary. The context hook below never awaits it.
      const response = await binding.rpc.call("context_boundary", JSON.parse(JSON.stringify({ messages, metadata })));
      const reference = contextContract<ReplayContextManifestRef>(response, "ReplayContextManifestRef");
      if (this.reference && reference.revision <= this.reference.revision) throw new Error("Stale context manifest revision");
      this.reference = reference;
    } catch (error) { this.fail(error, context); }
  }

  apply(messages: AgentMessage[], context: ExtensionContext): ReturnType<typeof applyContextManifest> {
    try {
      if (this.failure) throw this.failure;
      if (!this.reference) throw new Error("Replay context requires a prepared boundary manifest");
      return applyContextManifest(this.reference, messages);
    } catch (error) { return this.fail(error, context); }
  }

  compact(event: SessionBeforeCompactEvent, context: ExtensionContext): CompactionResult {
    if (this.failure) {
      throw this.failure;
    }
    event.signal.throwIfAborted();
    const entries = boundedBranch(event.branchEntries);
    const firstKeptEntryId = preservedEntryId(entries);
    // Native compaction retains original structured entries. The existing immutable
    // manifest, rather than a fabricated summary, reduces proven successful output.
    const messages = buildSessionContext(entries).messages.filter((message) => message.role !== "system");
    requireCompleteExchanges(messages);
    const { manifest } = this.apply(messages, context);
    const sourceLeafId = entries.at(-1)!.id;
    return { summary: `Receipt-aware context boundary for original leaf ${sourceLeafId}. Original conversation entries are retained.`,
      firstKeptEntryId, tokensBefore: event.preparation.tokensBefore,
      details: { rule: COMPACTION_REPLAY_ENTRY, sourceLeafId, projectionPrefixDigest: manifest.report.outputDigest } };
  }

  async refreshCompaction(entryId: string, context: ExtensionContext,
    append: (type: string, data: CompactionObservation) => void): Promise<void> {
    if (this.failure) {
      throw this.failure;
    }
    if (this.inFlight) {
      if (this.inFlight.entryId !== entryId) {
        throw new ReplayContextConflict("Another compaction replay observation is still pending");
      }
      return await this.inFlight.completion;
    }
    const completion = this.observeCompaction(entryId, context, append);
    this.inFlight = { entryId, completion };
    try {
      await completion;
    } finally {
      this.inFlight = undefined;
    }
  }

  private async observeCompaction(entryId: string, context: ExtensionContext,
    append: (type: string, data: CompactionObservation) => void): Promise<void> {
    const entries = boundedBranch(context.sessionManager.getBranch());
    const states = compactionObservations(entries);
    if ([...states.values()].includes("started")) {
      throw new Error("Interrupted compaction replay observation requires reconciliation");
    }
    if (states.get(entryId) === "completed") {
      return;
    }
    const latest = latestCompaction(entries);
    if (latest?.id !== entryId) {
      throw new ReplayContextConflict("Compaction replay differs from the current native branch");
    }
    append(COMPACTION_REPLAY_ENTRY, { compactionEntryId: entryId, status: "started" });
    await this.prepare(buildSessionContext(boundedBranch(context.sessionManager.getBranch())).messages, context);
    append(COMPACTION_REPLAY_ENTRY, { compactionEntryId: entryId, status: "completed" });
  }
}

function registerCompactionHooks(pi: ExtensionAPI, state: ContextManifestBinding): void {
  pi.on("session_before_compact", (event, context) => {
    if (state.binding.isStopped?.()) {
      return { cancel: true };
    }
    try {
      return { compaction: state.compact(event, context) };
    } catch (error) {
      // Pi catches thrown handlers and otherwise falls through to a model
      // summary. An explicit cancel is required before any provider call.
      state.recordFailure(error, context);
      return { cancel: true };
    }
  });
  pi.on("session_compact", async (event, context) => {
    try {
      await state.refreshCompaction(event.compactionEntry.id, context, (type, data) => pi.appendEntry(type, data));
    } catch (error) {
      state.fail(error, context);
    }
  });
}

/** Native hooks forward boundaries and synchronously apply the protected parent's projection. */
export function createReplayContextExtension(binding: ReplayContextBinding): ExtensionFactory {
  return (pi) => {
    const state = new ContextManifestBinding(binding);
    let started: SessionStartEvent["reason"] | undefined;
    let lastReport: string | undefined;
    pi.on("session_start", async (event, context) => {
      started = event.reason;
      lastReport = undefined;
      await state.start(context, (type, data) => pi.appendEntry(type, data));
    });
    pi.on("turn_end", async (event, context) => {
      if (!state.failure) await state.prepare(event.context.contextMessages, context);
    });
    pi.on("context", (event, context) => {
      const result = state.apply(event.messages, context);
      if (result.manifest.report.changed) {
        const report = JSON.stringify(result.manifest.report);
        if (report !== lastReport) {
          pi.appendEntry(REPLAY_CONTEXT_RULE, { sessionStart: started, ...result.manifest.report });
          lastReport = report;
        }
      }
      return { messages: result.messages };
    });
    if (binding.replayOnCompaction) {
      registerCompactionHooks(pi, state);
    } else if (binding.semanticCompaction) {
      pi.on("session_compact", async (_event, context) => {
        await state.prepare(buildSessionContext(context.sessionManager.getBranch()).messages, context);
      });
    }
  };
}
