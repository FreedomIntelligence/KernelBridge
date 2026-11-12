import { createHash } from "node:crypto";
import { SCHEMA } from "../generated/context.ts";
import type { ContextSource, JsonObject, Projection } from "../generated/context.ts";
import { validatedSnapshot } from "./json-snapshot.ts";
import { canonicalKey } from "../run/scoring/canonical.ts";
import { wireValidator } from "../clients/wire-schemas.ts";

type Writable<T> = { -readonly [Key in keyof T]: T[Key] };
/** Mutable selection state derived from the authoritative generated wire types. */
type SelectionContext = Writable<Omit<ContextSource, "projection">> & { projection: Writable<Projection> };
const validateSource = wireValidator<ContextSource>(SCHEMA.$id, "ContextSource");

export interface CompiledContext {
  readonly context: ContextSource;
  readonly canonical: string;
  readonly bytes: number;
  readonly sourceDigest: string;
  readonly compilerVersion: "kernelcat.context-compiler/v1";
}

export class ContextOverflow extends Error {}

function isRecord(value: unknown): value is JsonObject {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function wholeExchange(value: unknown): void {
  if (!isRecord(value) || !isRecord(value.assistant) || !Array.isArray(value.results)) {
    throw new Error("Context source contains an invalid exchange");
  }
  const content = value.assistant.content;
  if (!Array.isArray(content) || content.some((block) => !isRecord(block) ||
      (block.type !== "text" && block.type !== "toolCall" && block.type !== "thinking"))) {
    throw new Error("Context source exchange has invalid assistant content");
  }
  const calls = content.filter((block) => isRecord(block) && block.type === "toolCall");
  const results = value.results;
  if (calls.length !== results.length) throw new Error("Context source exchange is incomplete");
  calls.forEach((call, index) => {
    const result = results[index];
    if (!isRecord(call) || !isRecord(result) || result.tool_call_id !== call.id ||
        result.tool_name !== call.name || (result.status !== "completed" && result.status !== "failed")) {
      throw new Error("Context source exchange has an unmatched or unresolved tool result");
    }
  });
}

function sourceSnapshot(value: unknown): SelectionContext {
  const source = validatedSnapshot(value, validateSource, "Context source");
  const limit = source.policy.context_bytes;
  if (typeof limit !== "number" || !Number.isSafeInteger(limit) || limit <= 0) {
    throw new Error("Context source context_bytes must be a positive safe integer");
  }
  if (limit !== source.projection.context_byte_limit) {
    throw new Error("Context source policy and projection byte limits differ");
  }
  for (const turn of source.turns ?? []) wholeExchange(turn);
  return { ...source, projection: { ...source.projection } };
}

/** Covers JSON bytes and Python's extra zero in negative one-digit exponents. */
export function encodedBytes(value: unknown): number {
  const text = canonicalKey(value);
  let exponentPadding = 0;
  function visit(item: unknown): void {
    if (typeof item === "number" && /e-[1-9]$/.test(JSON.stringify(item))) exponentPadding += 1;
    else if (Array.isArray(item)) item.forEach(visit);
    else if (isRecord(item)) Object.values(item).forEach(visit);
  }
  visit(value);
  return Buffer.byteLength(text) + exponentPadding;
}

interface Selection {
  readonly field: "history" | "inputs" | "turns";
  readonly items: readonly JsonObject[];
  readonly sizes: readonly number[];
  start: number;
  readonly minimum: number;
  readonly omitted?: "history_omitted" | "turns_omitted";
}

function select(source: SelectionContext): void {
  const rules: readonly Pick<Selection, "field" | "minimum" | "omitted">[] = [
    { field: "history", minimum: 0, omitted: "history_omitted" },
    { field: "inputs", minimum: 1 },
    { field: "turns", minimum: 0, omitted: "turns_omitted" },
  ];
  const selections: Selection[] = rules.map((rule) => {
    const items = source[rule.field] ?? [];
    return { ...rule, items, sizes: items.map(encodedBytes), start: 0 };
  });
  let bytes = encodedBytes(source);
  const limit = source.policy.context_bytes as number;
  for (const selection of selections) {
    while (bytes > limit && selection.items.length - selection.start > selection.minimum) {
      const remaining = selection.items.length - selection.start;
      bytes -= selection.sizes[selection.start] + (remaining > 1 ? 1 : 0);
      selection.start += 1;
      if (selection.omitted) {
        const prior = source.projection[selection.omitted] as number | undefined ?? 0;
        const next = prior + 1;
        // The worker supplies omission counters even when no history is selected.
        if (!Object.hasOwn(source.projection, selection.omitted)) {
          throw new Error(`Context source is missing ${selection.omitted}`);
        }
        source.projection[selection.omitted] = next;
        bytes += String(next).length - String(prior).length;
      }
    }
    if (selection.field in source) source[selection.field] = selection.items.slice(selection.start);
  }
  if (bytes > limit) {
    throw new ContextOverflow("Mandatory task, policy, budget, profile and latest input exceed context_bytes");
  }
}

/** O(N log K) canonical sorting plus O(N) selection; each optional item is sized once. */
export function compileContext(value: unknown, options: { readonly harnessConversation?: boolean } = {}): CompiledContext {
  const source = sourceSnapshot(value);
  const sourceDigest = "sha256:" + createHash("sha256").update(canonicalKey(source)).digest("hex");
  if (options.harnessConversation) {
    // Pi supplies the effective conversation. Journal payloads remain available by receipt.
    source.projection.history_omitted += source.history.length;
    source.history = [];
    if (source.turns !== undefined) {
      source.projection.turns_omitted = (source.projection.turns_omitted ?? 0) + source.turns.length;
      source.turns = [];
    }
  }
  select(source);
  const canonical = canonicalKey(source);
  return {
    context: source,
    canonical,
    bytes: encodedBytes(source),
    sourceDigest,
    compilerVersion: "kernelcat.context-compiler/v1",
  };
}
