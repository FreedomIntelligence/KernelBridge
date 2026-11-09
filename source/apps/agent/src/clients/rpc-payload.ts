import { constants as bufferConstants } from "node:buffer";
import { createHash } from "node:crypto";
import { TextDecoder } from "node:util";
import type { ValidateFunction } from "ajv";
import { SCHEMA } from "../generated/rpc-payload.ts";
import type {
  PayloadBeginRequest, PayloadMethod, PayloadReadResult, PayloadRef, PayloadReference, PayloadWriteResult,
} from "../generated/rpc-payload.ts";
import { SCHEMA as CONTEXT_SCHEMA } from "../generated/context.ts";
import { SCHEMA as HARNESS_SCHEMA } from "../generated/harness.ts";
import type { JsonObject, ModelContextRequest } from "../generated/context.ts";
import { validatedSnapshot } from "../context/json-snapshot.ts";
import { SCHEMA as RUNTIME_SCHEMA } from "../generated/runtime.ts";
import { SCHEMA as KNOWLEDGE_SCHEMA } from "../generated/knowledge-runtime.ts";
import { SCHEMA as RSI_SCHEMA } from "../generated/rsi.ts";
import { wireValidator } from "./wire-schemas.ts";

export const DEFAULT_MAX_PAYLOAD_BYTES = 64 * 1024 * 1024;
export const MAX_AGGREGATE_BYTES = Math.min(bufferConstants.MAX_LENGTH, bufferConstants.MAX_STRING_LENGTH);
export const MAX_UPLOAD_BYTES = SCHEMA.$defs.PayloadBeginRequest.properties.bytes.maximum;
export const PAYLOAD_TRANSPORT = SCHEMA.$defs.PayloadReference.properties.transport.const;
// Method ceilings are part of PayloadBeginRequest's transport contract. Widening
// runtime uploads must not widen the existing model or native effect boundary.
const uploadLimits: Readonly<Record<PayloadMethod, number>> = {
  model: 8 * 1024 * 1024 + 4096,
  native_begin: 4198400,
  native_call: 4198400,
  native_settle: 4198400,
  prepare_run: MAX_UPLOAD_BYTES,
  freeze_run: MAX_UPLOAD_BYTES,
  snapshot_program: MAX_UPLOAD_BYTES,
  adopt_candidate: MAX_UPLOAD_BYTES,
  stop_run: MAX_UPLOAD_BYTES,
  knowledge_fence: MAX_UPLOAD_BYTES,
  prepare_trial: MAX_UPLOAD_BYTES,
  freeze_trial: MAX_UPLOAD_BYTES,
  knowledge_sync: MAX_UPLOAD_BYTES,
  rsi_record: 2 * 1024 * 1024,
  rsi_adapter: 2 * 1024 * 1024,
  rsi_apply: 2 * 1024 * 1024,
};
function validator<T>(schema: string, name: string): ValidateFunction<T> {
  return wireValidator<T>(schema, name);
}

const referenceValidator = validator<PayloadReference>(SCHEMA.$id, "PayloadReference");
const readValidator = validator<PayloadReadResult>(SCHEMA.$id, "PayloadReadResult");
const writeValidator = validator<PayloadWriteResult>(SCHEMA.$id, "PayloadWriteResult");
const resultValidator = validator<JsonObject>(CONTEXT_SCHEMA.$id, "JsonObject");
const modelValidator = validator<ModelContextRequest>(CONTEXT_SCHEMA.$id, "ModelContextRequest");
const nativeModelValidator = validator(HARNESS_SCHEMA.$id, "HarnessModelRequest");
const methodValidators = {
  native_begin: validator(HARNESS_SCHEMA.$id, "NativeBeginRequest"),
  native_call: validator(HARNESS_SCHEMA.$id, "NativeCallRequest"),
  native_settle: validator(HARNESS_SCHEMA.$id, "NativeSettleRequest"),
  prepare_run: validator(RUNTIME_SCHEMA.$id, "PrepareRunRequest"),
  freeze_run: validator(RUNTIME_SCHEMA.$id, "FreezeRunRequest"),
  snapshot_program: validator(RUNTIME_SCHEMA.$id, "SnapshotProgramRequest"),
  adopt_candidate: validator(RUNTIME_SCHEMA.$id, "AdoptCandidateRequest"),
  stop_run: validator(RUNTIME_SCHEMA.$id, "StopRunRequest"),
  knowledge_fence: validator(KNOWLEDGE_SCHEMA.$id, "KnowledgeFenceRequest"),
  prepare_trial: validator(RUNTIME_SCHEMA.$id, "PrepareTrialRequest"),
  freeze_trial: validator(RUNTIME_SCHEMA.$id, "FreezeTrialRequest"),
  knowledge_sync: validator(RUNTIME_SCHEMA.$id, "KnowledgeSyncRequest"),
  rsi_record: validator(RSI_SCHEMA.$id, "RecordWrite"),
  rsi_adapter: validator(RSI_SCHEMA.$id, "AdapterCall"),
  rsi_apply: validator(RSI_SCHEMA.$id, "SourceApply"),
};

export function supportsUpload(method: string): method is PayloadMethod {
  return (SCHEMA.$defs.PayloadMethod.enum as readonly string[]).includes(method);
}

export function validatePayloadLimit(limit: number): void {
  if (!Number.isSafeInteger(limit) || limit < 1 || limit > MAX_AGGREGATE_BYTES) {
    throw new Error(`Payload aggregate limit must be a positive safe integer at most ${MAX_AGGREGATE_BYTES}`);
  }
}

export function payloadReference(value: unknown, limit: number): PayloadReference {
  const reference = validatedSnapshot(value, referenceValidator, "RPC payload reference");
  if (reference.ref.bytes > limit) {
    throw new Error(`RPC payload advertises ${reference.ref.bytes} bytes; aggregate limit is ${limit} bytes`);
  }
  return reference;
}

function sameReference(actual: PayloadRef, expected: PayloadRef): void {
  if (actual.id !== expected.id || actual.bytes !== expected.bytes || actual.sha256 !== expected.sha256) {
    throw new Error("RPC payload reference changed during transfer");
  }
}

/** Reserve space for the bounded v3 envelope and generated reference/offset fields. */
export function payloadPageBytes(frameBytes: number): number {
  return Math.min(SCHEMA.$defs.PayloadReadRequest.properties.max_bytes.maximum, Math.floor((frameBytes - 1024) / 4) * 3);
}

export function payloadPage(value: unknown, ref: PayloadRef, offset: number, maximum: number): Buffer {
  const page = validatedSnapshot(value, readValidator, "RPC payload page");
  sameReference(page.ref, ref);
  if (page.offset !== offset) throw new Error("RPC payload page offset is not contiguous");
  const bytes = Buffer.from(page.data, "base64");
  if (bytes.toString("base64") !== page.data) throw new Error("RPC payload page is not strict base64");
  if (bytes.length < 1 || bytes.length > maximum || offset + bytes.length > ref.bytes) {
    throw new Error("RPC payload page length exceeds its requested range");
  }
  if (page.final !== (offset + bytes.length === ref.bytes)) throw new Error("RPC payload final marker disagrees with its length");
  return bytes;
}

export function decodedPayload(bytes: Buffer, ref: PayloadRef): JsonObject {
  if (bytes.length !== ref.bytes || createHash("sha256").update(bytes).digest("hex") !== ref.sha256) {
    throw new Error("RPC payload length or SHA256 mismatch");
  }
  const text = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  return validatedResult(JSON.parse(text));
}

export function validatedResult(value: unknown): JsonObject {
  return validatedSnapshot(value, resultValidator, "RPC result");
}

export interface PayloadUpload {
  readonly bytes: Buffer;
  readonly begin: PayloadBeginRequest;
}

export function effectUpload(method: PayloadMethod, value: unknown,
                             aggregateLimit = DEFAULT_MAX_PAYLOAD_BYTES): PayloadUpload {
  const validate = method === "model" ?
    (value !== null && typeof value === "object" && Object.hasOwn(value, "harness") ? nativeModelValidator : modelValidator) :
    methodValidators[method];
  const params = validatedSnapshot(value, validate, `${method} request`);
  const text = JSON.stringify(params);
  const length = Buffer.byteLength(text);
  const methodLimit = uploadLimits[method];
  if (length > methodLimit) {
    throw new Error(`RPC ${method} payload is ${length} bytes; upload limit is ${methodLimit} bytes`);
  }
  if (length > aggregateLimit) throw new Error(`RPC payload is ${length} bytes; aggregate limit is ${aggregateLimit} bytes`);
  const bytes = Buffer.from(text, "utf8");
  return { bytes, begin: { bytes: bytes.length, sha256: createHash("sha256").update(bytes).digest("hex"),
    ...(method === "model" ? {} : { method }) } };
}

export function uploadReference(value: unknown, begin: PayloadBeginRequest): PayloadRef {
  const { ref } = payloadReference(value, MAX_UPLOAD_BYTES);
  if (ref.bytes !== begin.bytes || ref.sha256 !== begin.sha256) throw new Error("RPC upload reference differs from its admitted bytes");
  return ref;
}

export function checkUploadWrite(value: unknown, ref: PayloadRef, nextOffset: number): void {
  const response = validatedSnapshot(value, writeValidator, "RPC upload write");
  sameReference(response.ref, ref);
  if (response.next_offset !== nextOffset) throw new Error("RPC upload acknowledgement offset differs from its write");
}
