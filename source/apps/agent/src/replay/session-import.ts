import { createHash } from "node:crypto";
import { constants } from "node:fs";
import { lstat, open, realpath } from "node:fs/promises";
import { dirname, isAbsolute, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { writeSessionOutput } from "./session-output.ts";

export const SESSION_IMPORT_LIMITS = Object.freeze({
  inputBytes: 64 * 1024 * 1024,
  lineBytes: 4 * 1024 * 1024,
  entries: 32768,
  observations: 65536,
  calls: 10000,
  messages: 20000,
  artifactCount: 128,
  manifestBytes: 1024 * 1024,
  artifactBytes: 64 * 1024 * 1024,
  totalArtifactBytes: 256 * 1024 * 1024,
  outputBytes: 32 * 1024 * 1024,
});

export interface SessionImportOptions {
  input: string;
  output: string;
  prefixLine?: number;
  divergenceLine?: number;
  artifactManifest?: string;
}

/** SHA-256 covers exactly [byte_start, byte_end), including a final LF if present. */
export interface RawReference {
  source_path: string;
  line_start: number;
  line_end: number;
  byte_start: number;
  byte_end: number;
  sha256: string;
  json_pointer: string;
}

type JsonObject = Record<string, unknown>;
type HandleState = "unknown" | "running" | "reported_completed" | "error";
type ExecutionState = Exclude<HandleState, "reported_completed"> | "error_and_running";

export interface HandleObservation {
  kind: "cell" | "process" | "job" | "task";
  id: string;
  state: HandleState;
  ref: RawReference;
}

export interface ResultEvidence {
  state: ExecutionState;
  reported_completion: boolean;
  error_signals: number;
  producer_exit_codes: number[];
  handles: HandleObservation[];
  parsed_json_values: number;
  unparsed_text_blocks: number;
  inspection_truncated: boolean;
}

export interface ImportedCall {
  call_id: string;
  tool: string;
  request_kind: string;
  request_provenance: "recorded" | "inherited";
  arguments_sha256: string;
  referenced_handles: HandleObservation[];
  request_refs: RawReference[];
  output_refs: RawReference[];
  output_sha256: string | null;
  delivery_state: "pending" | "returned";
  result: ResultEvidence;
}

export interface HistoricalMessage {
  id: string | null;
  parent_entry_id: string | null;
  role: string;
  channel: string | null;
  provenance: "recorded" | "inherited" | "compaction" | "event_message";
  authorization: "historical_only";
  content_sha256: string;
  duplicate_content_of: number | null;
  refs: RawReference[];
}

interface ImportedEntry {
  type: string;
  subtype: string | null;
  entry_id: string | null;
  parent_entry_id: string | null;
  ref: RawReference;
}

interface LineageObservation {
  kind: "session" | "fork";
  session_id: string | null;
  parent_session_id: string | null;
  parent_session_path: string | null;
  ref: RawReference;
}

interface CompactionObservation {
  first_kept_entry_id: string | null;
  ref: RawReference;
}

export interface ArtifactObservation {
  path: string;
  expected_sha256: string;
  observed_sha256: string | null;
  bytes: number | null;
  status: "verified" | "unavailable" | "conflict";
  reason: string | null;
  historical_availability: "unknown";
  manifest_ref: RawReference;
}

export interface SessionImportSummary {
  analysis_format: "kernelcat.session-import/v1";
  authority: "analysis_only";
  source: {
    path: string;
    bytes: number;
    lines: number;
    sha256: string;
    prefix_sha256: string;
    prefix_bytes: number;
    format: "codex" | "native_pi" | "mixed" | "unknown";
  };
  replay: {
    prefix_line: number;
    divergence_line: number | null;
    effective_last_line: number;
    excluded_lines: number;
    suffix_reused: false;
    net_performance_measured: false;
    causal_effect_measured: false;
    historical_messages_authorize_effects: false;
  };
  counts: {
    entries: number;
    calls: number;
    outputs: number;
    duplicate_call_deliveries: number;
    duplicate_output_deliveries: number;
    messages: number;
    duplicate_message_deliveries: number;
    compactions: number;
    artifacts: number;
  };
  entries: ImportedEntry[];
  calls: ImportedCall[];
  orphan_outputs: { call_id: string; sha256: string; refs: RawReference[]; result: ResultEvidence }[];
  messages: HistoricalMessage[];
  lineage: LineageObservation[];
  compactions: CompactionObservation[];
  artifacts: ArtifactObservation[];
  current_view: {
    pending_call_ids: string[];
    pending_handles: HandleObservation[];
    historical_user_message_indexes: number[];
    model_workspaces: string[];
  };
}

interface OutputDelivery {
  sha256: string;
  refs: RawReference[];
  result: ResultEvidence;
}

interface ImportState {
  entries: ImportedEntry[];
  calls: Map<string, ImportedCall>;
  callNamespaces: Map<string, string | null>;
  directResultHandles: WeakSet<HandleObservation>;
  outputs: Map<string, OutputDelivery>;
  messages: HistoricalMessage[];
  messageIds: Map<string, { fingerprint: string; index: number }>;
  messageContents: Map<string, number>;
  entryIds: Map<string, string>;
  lineage: LineageObservation[];
  compactions: CompactionObservation[];
  workspaces: Set<string>;
  formats: Set<"codex" | "native_pi">;
  duplicateCalls: number;
  duplicateOutputs: number;
  duplicateMessages: number;
  observations: number;
}

const decoder = new TextDecoder("utf-8", { fatal: true });
const identifierPattern = /^[A-Za-z0-9_.:/-]{1,512}$/;
const runningStates = new Set(["running", "pending", "accepted", "queued", "in_progress", "started"]);
const errorStates = new Set(["failed", "failure", "error", "cancelled", "canceled", "rejected"]);
const completeStates = new Set(["completed", "succeeded", "success"]);

function sha256(value: string | Uint8Array): string {
  return createHash("sha256").update(value).digest("hex");
}

function object(value: unknown, label: string): JsonObject {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`${label} must be an object`);
  }
  return value as JsonObject;
}

function maybeObject(value: unknown): JsonObject | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as JsonObject : null;
}

function identifier(value: unknown, label: string): string {
  if (typeof value !== "string" || !identifierPattern.test(value)) {
    throw new Error(`${label} requires a nonempty bounded identifier`);
  }
  return value;
}

function optionalIdentifier(value: unknown, label: string): string | null {
  return value === undefined || value === null ? null : identifier(value, label);
}

function absolutePath(value: unknown, label: string): string {
  if (typeof value !== "string" || value.length > 4096 || value.includes("\0") || !isAbsolute(value)) {
    throw new Error(`${label} requires an absolute path`);
  }
  return resolve(value);
}

function validateStructure(value: unknown): void {
  const pending = [{ value, depth: 0 }];
  let nodes = 0;
  while (pending.length > 0) {
    const item = pending.pop()!;
    if (++nodes > 100000 || item.depth > 64) {
      throw new Error("JSON structure exceeds the bounded depth or node count");
    }
    if (typeof item.value === "number" && (!Number.isFinite(item.value)
      || (Number.isInteger(item.value) && !Number.isSafeInteger(item.value)))) {
      throw new Error("JSON number exceeds the finite, safe-integer boundary");
    }
    if (item.value !== null && typeof item.value === "object") {
      const values = Array.isArray(item.value) ? item.value : Object.values(item.value);
      if (nodes + pending.length + values.length > 100000) {
        throw new Error("JSON structure exceeds the bounded depth or node count");
      }
      for (const child of values) {
        pending.push({ value: child, depth: item.depth + 1 });
      }
    }
  }
}

function canonical(value: unknown): string {
  if (Array.isArray(value)) {
    return `[${value.map(canonical).join(",")}]`;
  }
  if (value !== null && typeof value === "object") {
    return `{${Object.keys(value).sort().map((key) =>
      `${JSON.stringify(key)}:${canonical((value as JsonObject)[key])}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

function parseJson(bytes: Uint8Array, label: string): unknown {
  let value: unknown;
  try {
    value = JSON.parse(decoder.decode(bytes));
  } catch {
    // A JSON parser's exception can contain transcript text or credentials.
    throw new Error(`${label} is not valid UTF-8 JSON`);
  }
  validateStructure(value);
  return value;
}

async function readFrozen(path: string, limit: number): Promise<Buffer> {
  const beforePath = await lstat(path, { bigint: true });
  if (!beforePath.isFile() || beforePath.isSymbolicLink() || await realpath(path) !== path) {
    throw new Error("Frozen input requires a canonical regular file without symlink traversal");
  }
  const file = await open(path, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
  try {
    const before = await file.stat({ bigint: true });
    if (!before.isFile() || before.size > BigInt(limit)) {
      throw new Error(`Frozen input exceeds its ${limit}-byte regular-file bound`);
    }
    if (beforePath.ino !== before.ino || beforePath.dev !== before.dev) {
      throw new Error("Frozen input path changed before it was opened");
    }
    const chunks: Buffer[] = [];
    let size = 0;
    while (true) {
      const chunk = Buffer.alloc(Math.min(65536, limit + 1 - size));
      const { bytesRead } = await file.read(chunk, 0, chunk.length, null);
      if (bytesRead === 0) break;
      chunks.push(chunk.subarray(0, bytesRead));
      size += bytesRead;
      if (size > limit) throw new Error(`Frozen input exceeds its ${limit}-byte bound`);
    }
    const after = await file.stat({ bigint: true });
    const afterPath = await lstat(path, { bigint: true });
    if (before.size !== BigInt(size) || before.size !== after.size
      || before.mtimeNs !== after.mtimeNs || before.ctimeNs !== after.ctimeNs
      || afterPath.ino !== after.ino || afterPath.dev !== after.dev) {
      throw new Error("Frozen input changed while it was being read");
    }
    return Buffer.concat(chunks, size);
  } finally {
    await file.close();
  }
}

function rawRef(bytes: Buffer, path: string, start: number, end: number, line: number): RawReference {
  return { source_path: path, line_start: line, line_end: line, byte_start: start, byte_end: end,
    sha256: sha256(bytes.subarray(start, end)), json_pointer: "" };
}

function childRef(ref: RawReference, pointer: string): RawReference {
  return { ...ref, json_pointer: ref.json_pointer + pointer };
}

function lineRanges(bytes: Buffer): { start: number; end: number }[] {
  const result: { start: number; end: number }[] = [];
  let start = 0;
  while (start < bytes.length) {
    const newline = bytes.indexOf(10, start);
    const end = newline === -1 ? bytes.length : newline + 1;
    if (end - start > SESSION_IMPORT_LIMITS.lineBytes) {
      throw new Error(`Session line ${result.length + 1} exceeds the line-byte bound`);
    }
    result.push({ start, end });
    if (result.length > SESSION_IMPORT_LIMITS.entries) throw new Error("Session exceeds the line-count bound");
    start = end;
  }
  if (result.length === 0) throw new Error("Session input is empty");
  return result;
}

function positiveLine(value: number | undefined, fallback: number, label: string): number {
  if (value === undefined) return fallback;
  if (!Number.isSafeInteger(value) || value < 1) throw new Error(`${label} requires a positive safe integer`);
  return value;
}

function createState(): ImportState {
  return { entries: [], calls: new Map(), callNamespaces: new Map(), directResultHandles: new WeakSet(),
    outputs: new Map(), messages: [],
    messageIds: new Map(), messageContents: new Map(), entryIds: new Map(), lineage: [],
    compactions: [], workspaces: new Set(), formats: new Set(), duplicateCalls: 0,
    duplicateOutputs: 0, duplicateMessages: 0, observations: 0 };
}

function observe(state: ImportState): void {
  if (++state.observations > SESSION_IMPORT_LIMITS.observations) {
    throw new Error("Session exceeds the normalized-observation bound");
  }
}

function emptyEvidence(): ResultEvidence {
  return { state: "unknown", reported_completion: false, error_signals: 0, producer_exit_codes: [], handles: [],
    parsed_json_values: 0, unparsed_text_blocks: 0, inspection_truncated: false };
}

function reportedState(value: unknown): HandleState {
  if (typeof value !== "string") return "unknown";
  if (runningStates.has(value)) return "running";
  if (errorStates.has(value)) return "error";
  return completeStates.has(value) ? "reported_completed" : "unknown";
}

function objectHandles(value: JsonObject, ref: RawReference): HandleObservation[] {
  const result: HandleObservation[] = [];
  const names = { cell_id: "cell", session_id: "process", job_id: "job", task_id: "task" } as const;
  for (const [key, kind] of Object.entries(names)) {
    const id = value[key];
    if (id === undefined || id === null) continue;
    const text = typeof id === "number" && Number.isSafeInteger(id) ? String(id) : id;
    if (typeof text !== "string" || !identifierPattern.test(text)) continue;
    let state = reportedState(value.status ?? value.state);
    if (kind === "process" && state === "unknown" && value.exit_code == null) state = "running";
    result.push({ kind, id: text, state, ref: childRef(ref, `/${key}`) });
  }
  return result;
}

/** Inspect structured observations only. JavaScript source and wrapper success are never evaluated. */
function inspectResult(value: unknown, ref: RawReference, directHandles: WeakSet<HandleObservation>): ResultEvidence {
  const result = emptyEvidence();
  const pending = [{ value, ref, depth: 0, direct: true }];
  let inspected = 0;
  let running = false;
  while (pending.length > 0) {
    const item = pending.pop()!;
    if (++inspected > 8192 || item.depth > 12) {
      result.inspection_truncated = true;
      break;
    }
    if (typeof item.value === "string") {
      const text = item.value.trim();
      const match = /^Script running with cell ID ([A-Za-z0-9_.:-]{1,512})(?:\s|$)/.exec(text);
      if (match) {
        running = true;
        result.handles.push({ kind: "cell", id: match[1]!, state: "running", ref: item.ref });
      }
      if ((text.startsWith("{") || text.startsWith("[")) && result.parsed_json_values < 32) {
        try {
          const parsed: unknown = JSON.parse(text);
          validateStructure(parsed);
          result.parsed_json_values++;
          pending.push({ value: parsed, ref: childRef(item.ref, "/$json"), depth: item.depth + 1, direct: item.direct });
          continue;
        } catch { /* Opaque nested output remains unknown and retains its original raw reference. */ }
      }
      result.unparsed_text_blocks++;
    } else if (Array.isArray(item.value)) {
      for (let index = item.value.length - 1; index >= 0; index--) {
        pending.push({ value: item.value[index], ref: childRef(item.ref, `/${index}`), depth: item.depth + 1, direct: false });
      }
    } else if (item.value !== null && typeof item.value === "object") {
      const record = item.value as JsonObject;
      const state = reportedState(record.status ?? record.state);
      const codes = [record.exit_code, record.exitCode, record.returncode]
        .filter((code): code is number => typeof code === "number" && Number.isSafeInteger(code));
      result.producer_exit_codes.push(...codes);
      if (state === "error" || record.isError === true || record.is_error === true
        || codes.some((code) => code !== 0)) result.error_signals++;
      result.reported_completion ||= state === "reported_completed";
      running ||= state === "running";
      const handles = objectHandles(record, item.ref);
      result.handles.push(...handles);
      if (item.direct) handles.forEach((handle) => directHandles.add(handle));
      for (const [key, child] of Object.entries(record).reverse()) {
        pending.push({ value: child, ref: childRef(item.ref, `/${escapePointer(key)}`), depth: item.depth + 1, direct: false });
      }
    }
  }
  running ||= result.handles.some((handle) => handle.state === "running");
  result.state = result.error_signals > 0 ? (running ? "error_and_running" : "error")
    : running ? "running" : "unknown";
  result.producer_exit_codes = [...new Set(result.producer_exit_codes)].sort((a, b) => a - b);
  return result;
}

function escapePointer(value: string): string {
  return value.replaceAll("~", "~0").replaceAll("/", "~1");
}

function referencedHandles(value: unknown, ref: RawReference): HandleObservation[] {
  let parsed = value;
  let parsedRef = ref;
  if (typeof value === "string") {
    try { parsed = parseJson(Buffer.from(value), "Call arguments"); }
    catch { return []; }
    parsedRef = childRef(ref, "/$json");
  }
  const record = maybeObject(parsed);
  if (record === null) return [];
  return objectHandles(record, parsedRef).map((handle) => ({ ...handle, state: "unknown" }));
}

function addCall(state: ImportState, payload: JsonObject, ref: RawReference,
  inherited = false, native = false): void {
  observe(state);
  const id = identifier(native ? payload.id : payload.call_id, `Call at line ${ref.line_start}`);
  const toolName = identifier(payload.name, `Tool at line ${ref.line_start}`);
  const namespace = optionalIdentifier(payload.namespace, `Tool namespace at line ${ref.line_start}`);
  const name = namespace === null ? toolName : `${namespace}.${toolName}`;
  const argumentKey = native || payload.type === "function_call" ? "arguments" : "input";
  if (!(argumentKey in payload)) throw new Error(`Call arguments missing at line ${ref.line_start}`);
  const digest = sha256(canonical(payload[argumentKey]));
  const previous = state.calls.get(id);
  if (previous) {
    if (previous.tool !== name || state.callNamespaces.get(id) !== namespace
      || previous.arguments_sha256 !== digest || previous.request_kind !== payload.type) {
      throw new Error(`Conflicting call identity at line ${ref.line_start}`);
    }
    previous.request_refs.push(ref);
    state.duplicateCalls++;
    return;
  }
  if (state.calls.size >= SESSION_IMPORT_LIMITS.calls) throw new Error("Session exceeds the call-count bound");
  state.callNamespaces.set(id, namespace);
  state.calls.set(id, { call_id: id, tool: name, request_kind: String(payload.type),
    request_provenance: inherited ? "inherited" : "recorded", arguments_sha256: digest,
    referenced_handles: referencedHandles(payload[argumentKey], childRef(ref, `/${argumentKey}`)),
    request_refs: [ref], output_refs: [], output_sha256: null, delivery_state: "pending", result: emptyEvidence() });
}

function addOutput(state: ImportState, idValue: unknown, value: unknown, ref: RawReference): void {
  observe(state);
  const id = identifier(idValue, `Output at line ${ref.line_start}`);
  const digest = sha256(canonical(value));
  const previous = state.outputs.get(id);
  if (previous) {
    if (previous.sha256 !== digest) throw new Error(`Conflicting output identity at line ${ref.line_start}`);
    previous.refs.push(ref);
    state.duplicateOutputs++;
    return;
  }
  if (state.outputs.size >= SESSION_IMPORT_LIMITS.calls) throw new Error("Session exceeds the output-count bound");
  state.outputs.set(id, { sha256: digest, refs: [ref], result: inspectResult(value, ref, state.directResultHandles) });
}

function addMessage(state: ImportState, payload: JsonObject, ref: RawReference,
  provenance: HistoricalMessage["provenance"], parent: unknown = null): void {
  observe(state);
  const id = optionalIdentifier(payload.id, `Message at line ${ref.line_start}`);
  const parentId = optionalIdentifier(parent, `Message parent at line ${ref.line_start}`);
  const role = identifier(payload.role, `Message role at line ${ref.line_start}`);
  const channel = optionalIdentifier(payload.channel, `Message channel at line ${ref.line_start}`);
  if (!("content" in payload)) throw new Error(`Message content missing at line ${ref.line_start}`);
  const content = sha256(canonical(payload.content));
  const fingerprint = sha256(canonical([parentId, role, channel, content]));
  const previous = id === null ? undefined : state.messageIds.get(id);
  if (previous) {
    if (previous.fingerprint !== fingerprint) throw new Error(`Conflicting message identity at line ${ref.line_start}`);
    state.messages[previous.index]!.refs.push(ref);
    state.duplicateMessages++;
    return;
  }
  if (state.messages.length >= SESSION_IMPORT_LIMITS.messages) throw new Error("Session exceeds the message-count bound");
  const index = state.messages.length;
  const duplicate = state.messageContents.get(fingerprint) ?? null;
  state.messages.push({ id, parent_entry_id: parentId, role, channel, provenance,
    authorization: "historical_only", content_sha256: content, duplicate_content_of: duplicate, refs: [ref] });
  if (id !== null) state.messageIds.set(id, { fingerprint, index });
  if (duplicate === null) state.messageContents.set(fingerprint, index);
}

function payloadObservation(state: ImportState, payload: JsonObject, ref: RawReference, inherited = false): void {
  switch (payload.type) {
    case "function_call":
    case "custom_tool_call":
      addCall(state, payload, ref, inherited);
      break;
    case "function_call_output":
    case "custom_tool_call_output":
      if (!("output" in payload)) throw new Error(`Tool output missing at line ${ref.line_start}`);
      addOutput(state, payload.call_id, payload.output, childRef(ref, "/output"));
      break;
    case "message":
      addMessage(state, payload, ref, inherited ? "inherited" : "recorded");
      break;
  }
}

function nativeMessage(state: ImportState, entry: JsonObject, ref: RawReference): void {
  const message = object(entry.message, `Native Pi message at line ${ref.line_start}`);
  const messageRef = childRef(ref, "/message");
  addMessage(state, { ...message, id: entry.id }, messageRef, "recorded", entry.parentId);
  if (message.role === "toolResult") {
    addOutput(state, message.toolCallId, message, messageRef);
  } else if (message.role === "assistant" && Array.isArray(message.content)) {
    message.content.forEach((part, index) => {
      const content = maybeObject(part);
      if (content?.type === "toolCall") {
        addCall(state, content, childRef(messageRef, `/content/${index}`), false, true);
      }
    });
  }
}

function lineageObservation(state: ImportState, payload: JsonObject, ref: RawReference,
  kind: LineageObservation["kind"]): void {
  const source = maybeObject(payload.source);
  const spawn = maybeObject(maybeObject(source?.subagent)?.thread_spawn);
  const parent = payload.parent_session_id ?? payload.forked_from_id ?? payload.forked_from_session_id
    ?? payload.parentSessionId ?? payload.parent_thread_id ?? spawn?.parent_thread_id;
  const parentPath = payload.parentSession;
  state.lineage.push({ kind, session_id: optionalIdentifier(payload.session_id ?? payload.id, "Session identity"),
    parent_session_id: optionalIdentifier(parent, "Parent session identity"),
    parent_session_path: parentPath == null ? null : absolutePath(parentPath, "Parent session path"), ref });
  if (payload.cwd !== undefined) state.workspaces.add(absolutePath(payload.cwd, "Model workspace"));
}

function compactionObservation(state: ImportState, payload: JsonObject, ref: RawReference): void {
  state.compactions.push({ first_kept_entry_id: optionalIdentifier(payload.firstKeptEntryId, "Compaction anchor"), ref });
  if (payload.summary !== undefined || payload.message !== undefined) {
    addMessage(state, { role: "summary", content: payload.summary ?? payload.message }, ref, "compaction");
  }
  if (Array.isArray(payload.replacement_history)) {
    payload.replacement_history.forEach((item, index) => payloadObservation(state,
      object(item, "Compaction replacement"), childRef(ref, `/replacement_history/${index}`), true));
  }
}

function recordObservation(state: ImportState, record: JsonObject, ref: RawReference): void {
  const type = identifier(record.type, `Entry type at line ${ref.line_start}`);
  const payload = maybeObject(record.payload);
  const native = type === "session" || (type === "message" && "message" in record)
    || type === "compaction" || type === "branch_summary";
  const entryId = native ? optionalIdentifier(record.id, "Native entry identity") : null;
  const parentId = native ? optionalIdentifier(record.parentId, "Native parent identity") : null;
  if (entryId !== null && type !== "session") {
    const digest = sha256(canonical(record));
    const previous = state.entryIds.get(entryId);
    if (previous && previous !== digest) throw new Error(`Conflicting native entry identity at line ${ref.line_start}`);
    state.entryIds.set(entryId, digest);
  }
  state.entries.push({ type, subtype: optionalIdentifier(payload?.type, "Payload type"),
    entry_id: entryId, parent_entry_id: parentId, ref });
  if (native) state.formats.add("native_pi");
  if (["session_meta", "response_item", "function_call", "function_call_output",
    "custom_tool_call", "custom_tool_call_output"].includes(type)) state.formats.add("codex");
  switch (type) {
    case "session_meta":
      lineageObservation(state, object(record.payload, "Session metadata"), childRef(ref, "/payload"), "session");
      break;
    case "session":
      lineageObservation(state, record, ref, "session");
      break;
    case "fork":
      lineageObservation(state, payload ?? record, payload ? childRef(ref, "/payload") : ref, "fork");
      break;
    case "response_item":
      payloadObservation(state, object(record.payload, "Response item"), childRef(ref, "/payload"));
      break;
    case "message":
      if (native) nativeMessage(state, record, ref);
      else payloadObservation(state, record, ref);
      break;
    case "compacted":
      compactionObservation(state, object(record.payload, "Compaction"), childRef(ref, "/payload"));
      break;
    case "compaction":
    case "branch_summary":
      compactionObservation(state, record, ref);
      break;
    case "turn_context":
      if (payload?.cwd !== undefined) state.workspaces.add(absolutePath(payload.cwd, "Model workspace"));
      break;
    case "event_msg":
      if (payload?.type === "user_message" && "message" in payload) {
        addMessage(state, { role: "user", content: payload.message }, childRef(ref, "/payload"), "event_message");
      }
      break;
    default: payloadObservation(state, record, ref);
  }
  if (state.workspaces.size > 128) throw new Error("Session exceeds the workspace-count bound");
}

async function forbiddenOutputRoots(input: string, workspaces: Set<string>): Promise<string[]> {
  const sourceRoot = await realpath(fileURLToPath(new URL("../../../../", import.meta.url)));
  const forbidden = [sourceRoot, dirname(input)];
  for (const workspace of workspaces) {
    try {
      forbidden.push(await realpath(workspace));
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
      forbidden.push(workspace);
    }
  }
  return forbidden;
}

interface ArtifactPin { path: string; sha256: string; ref: RawReference }

function artifactPins(manifest: JsonObject, ref: RawReference): ArtifactPin[] {
  let entries: { value: unknown; pointer: string }[];
  if (Array.isArray(manifest.artifacts)) {
    entries = manifest.artifacts.map((value, index) => ({ value, pointer: `/artifacts/${index}` }));
  } else if (maybeObject(manifest.references)) {
    entries = Object.entries(manifest.references as JsonObject).map(([path, digest]) =>
      ({ value: { path, sha256: digest }, pointer: `/references/${escapePointer(path)}` }));
  } else {
    throw new Error("Artifact manifest requires artifacts:[{path,sha256}] or explicit references:{path:sha256}");
  }
  if (entries.length > SESSION_IMPORT_LIMITS.artifactCount) throw new Error("Artifact manifest exceeds the entry-count bound");
  const seen = new Set<string>();
  return entries.map(({ value, pointer }) => {
    const item = object(value, "Artifact pin");
    const path = absolutePath(item.path, "Artifact pin");
    if (seen.has(path)) throw new Error("Artifact manifest repeats a path");
    seen.add(path);
    if (typeof item.sha256 !== "string" || !/^[0-9a-f]{64}$/.test(item.sha256)) {
      throw new Error("Artifact pin requires a lowercase SHA-256");
    }
    return { path, sha256: item.sha256, ref: childRef(ref, pointer) };
  });
}

async function verifyArtifact(pin: ArtifactPin, remaining: number): Promise<ArtifactObservation> {
  const result: ArtifactObservation = { path: pin.path, expected_sha256: pin.sha256,
    observed_sha256: null, bytes: null, status: "unavailable", reason: null,
    historical_availability: "unknown", manifest_ref: pin.ref };
  try {
    const stat = await lstat(pin.path);
    if (stat.isSymbolicLink() || !stat.isFile() || await realpath(pin.path) !== pin.path) {
      result.reason = "not_canonical_regular_file";
      return result;
    }
    if (stat.size > Math.min(remaining, SESSION_IMPORT_LIMITS.artifactBytes)) {
      result.reason = "artifact_byte_bound";
      return result;
    }
    const bytes = await readFrozen(pin.path, Math.min(remaining, SESSION_IMPORT_LIMITS.artifactBytes));
    result.bytes = bytes.length;
    result.observed_sha256 = sha256(bytes);
    result.status = result.observed_sha256 === pin.sha256 ? "verified" : "conflict";
    result.reason = result.status === "conflict" ? "hash_mismatch" : null;
  } catch (error) {
    const code = (error as NodeJS.ErrnoException).code;
    result.reason = code === "ENOENT" ? "missing" : code === "EACCES" || code === "EPERM"
      ? "unreadable" : "unstable_or_unreadable_bytes";
  }
  return result;
}

async function resolveArtifacts(value: string | undefined): Promise<ArtifactObservation[]> {
  if (value === undefined) return [];
  const path = absolutePath(value, "Artifact manifest");
  const bytes = await readFrozen(path, SESSION_IMPORT_LIMITS.manifestBytes);
  const ref = rawRef(bytes, path, 0, bytes.length, 1);
  ref.line_end = Math.max(1, bytes.toString("utf8").split("\n").length - (bytes.at(-1) === 10 ? 1 : 0));
  const pins = artifactPins(object(parseJson(bytes, "Artifact manifest"), "Artifact manifest"), ref);
  const result: ArtifactObservation[] = [];
  let remaining = SESSION_IMPORT_LIMITS.totalArtifactBytes;
  for (const pin of pins) {
    const observation = await verifyArtifact(pin, remaining);
    remaining -= observation.bytes ?? 0;
    result.push(observation);
  }
  return result;
}

function linkCalls(state: ImportState): SessionImportSummary["orphan_outputs"] {
  const orphans: SessionImportSummary["orphan_outputs"] = [];
  for (const [id, output] of state.outputs) {
    const call = state.calls.get(id);
    if (!call) {
      orphans.push({ call_id: id, ...output });
      continue;
    }
    call.output_refs = output.refs;
    call.output_sha256 = output.sha256;
    call.delivery_state = "returned";
    call.result = output.result;
  }
  return orphans;
}

function handleIdentity(state: ImportState, callId: string, handle: HandleObservation): string {
  const namespace = state.callNamespaces.get(callId) ?? null;
  // Nested observations and absent namespaces cannot establish an effect owner.
  const owner = namespace !== null && state.directResultHandles.has(handle)
    ? ["namespace", namespace]
    : ["observation", callId, handle.ref.byte_start, handle.ref.byte_end, handle.ref.json_pointer];
  return canonical([owner, handle.kind, handle.id]);
}

function currentView(state: ImportState): SessionImportSummary["current_view"] {
  const handles = new Map<string, HandleObservation>();
  // Map insertion follows original output order; wall-clock timestamps are not ordering authority.
  for (const [callId, output] of state.outputs) {
    for (const handle of output.result.handles) handles.set(handleIdentity(state, callId, handle), handle);
  }
  return {
    pending_call_ids: [...state.calls.values()].filter((call) => {
      if (call.delivery_state === "pending") return true;
      if (call.result.state !== "running" && call.result.state !== "error_and_running") return false;
      if (call.result.handles.length === 0) return true;
      return call.result.handles.some((handle) => {
        const latest = handles.get(handleIdentity(state, call.call_id, handle))!;
        return latest.state === "running" || latest.state === "unknown";
      });
    }).map((call) => call.call_id),
    pending_handles: [...handles.values()].filter((handle) => handle.state === "unknown" || handle.state === "running"),
    historical_user_message_indexes: state.messages.flatMap((message, index) => message.role === "user" ? [index] : []),
    model_workspaces: [...state.workspaces].sort(),
  };
}

async function writeReport(path: string, forbidden: string[], summary: SessionImportSummary): Promise<void> {
  const bytes = Buffer.from(canonical(summary) + "\n");
  await writeSessionOutput(path, bytes, SESSION_IMPORT_LIMITS.outputBytes, forbidden);
}

/** Read-only forensic projection. No transcript commands, model calls, journals or SDK imports. */
export async function importSession(options: SessionImportOptions): Promise<SessionImportSummary> {
  const input = absolutePath(options.input, "Input");
  const bytes = await readFrozen(input, SESSION_IMPORT_LIMITS.inputBytes);
  const lines = lineRanges(bytes);
  const prefix = positiveLine(options.prefixLine, lines.length, "Prefix line");
  const divergence = positiveLine(options.divergenceLine, lines.length + 1, "Divergence line");
  if (prefix > lines.length || divergence > lines.length + 1) throw new Error("Replay boundary exceeds the input line count");
  const last = Math.min(prefix, divergence - 1);
  const prefixBytes = last === 0 ? 0 : lines[last - 1]!.end;
  const state = createState();
  for (let index = 0; index < last; index++) {
    const { start, end } = lines[index]!;
    if (bytes.subarray(start, end).every((byte) => byte === 10 || byte === 13 || byte === 32 || byte === 9)) continue;
    const ref = rawRef(bytes, input, start, end, index + 1);
    recordObservation(state, object(parseJson(bytes.subarray(start, end), `Session line ${index + 1}`), "Session entry"), ref);
  }
  const output = absolutePath(options.output, "Output");
  const forbidden = await forbiddenOutputRoots(input, state.workspaces);
  const artifacts = await resolveArtifacts(options.artifactManifest);
  const orphans = linkCalls(state);
  const formats = [...state.formats];
  const summary: SessionImportSummary = {
    analysis_format: "kernelcat.session-import/v1", authority: "analysis_only",
    source: { path: input, bytes: bytes.length, lines: lines.length, sha256: sha256(bytes),
      prefix_sha256: sha256(bytes.subarray(0, prefixBytes)), prefix_bytes: prefixBytes,
      format: formats.length === 2 ? "mixed" : formats[0] ?? "unknown" },
    replay: { prefix_line: prefix, divergence_line: options.divergenceLine ?? null, effective_last_line: last,
      excluded_lines: lines.length - last, suffix_reused: false, net_performance_measured: false,
      causal_effect_measured: false, historical_messages_authorize_effects: false },
    counts: { entries: state.entries.length, calls: state.calls.size, outputs: state.outputs.size,
      duplicate_call_deliveries: state.duplicateCalls, duplicate_output_deliveries: state.duplicateOutputs,
      messages: state.messages.length, duplicate_message_deliveries: state.duplicateMessages,
      compactions: state.compactions.length, artifacts: artifacts.length },
    entries: state.entries, calls: [...state.calls.values()], orphan_outputs: orphans, messages: state.messages,
    lineage: state.lineage, compactions: state.compactions, artifacts, current_view: currentView(state),
  };
  await writeReport(output, forbidden, summary);
  return summary;
}
