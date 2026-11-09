// Finite declarations only; the owning transport must establish outcome and retry safety.
import { Ajv2020 } from "ajv/dist/2020.js";
import { SCHEMA } from "../generated/errors.ts";
import type { LabErrorEnvelope } from "../generated/errors.ts";
import { SCHEMA as TASK_SCHEMA } from "../generated/task-contract.ts";
import { validatedSnapshot } from "../context/json-snapshot.ts";

const validator = new Ajv2020({ strict: true, ownProperties: true })
  .addSchema(TASK_SCHEMA)
  .compile<LabErrorEnvelope>(SCHEMA);

export function validateLabError(value: unknown): LabErrorEnvelope {
  return validatedSnapshot(value, validator, "LabErrorEnvelope");
}
