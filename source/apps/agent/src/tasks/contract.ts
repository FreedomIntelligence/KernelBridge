// Wire validation is pure; scheduling, effects and scoring have separate owners.
import { createHash } from "node:crypto";
import { Ajv2020 } from "ajv/dist/2020.js";
import { validatedSnapshot } from "../context/json-snapshot.ts";
import { SCHEMA } from "../generated/task-contract.ts";
import type {
  ScoringResultSet, TaskContract, TaskContractSource,
} from "../generated/task-contract.ts";

const ajv = new Ajv2020({ strict: true, ownProperties: true }).addSchema(SCHEMA);
const taskValidator = ajv.compile<TaskContract>({ $ref: `${SCHEMA.$id}#/$defs/TaskContract` });
const scoringValidator = ajv.compile<ScoringResultSet>({ $ref: `${SCHEMA.$id}#/$defs/ScoringResultSet` });

export function validateTaskContract(value: unknown): TaskContract {
  return validatedSnapshot(value, taskValidator, "TaskContract");
}

export function validateScoringResultSet(value: unknown): ScoringResultSet {
  return validatedSnapshot(value, scoringValidator, "ScoringResultSet");
}

export function loadTaskContract(source: Uint8Array): TaskContractSource {
  let raw: Uint8Array;
  let value: unknown;
  try {
    raw = new Uint8Array(source);
    // Retain any BOM so JSON.parse rejects it, matching Python's strict UTF-8.
    const text = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(raw);
    value = JSON.parse(text);
  } catch (error) {
    throw new Error(`Cannot decode TaskContract UTF-8 JSON source: ${String(error)}`, { cause: error });
  }
  const contract = validateTaskContract(value);
  return { source_digest: `sha256:${createHash("sha256").update(raw).digest("hex")}`, contract };
}
