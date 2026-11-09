// Validation retains supplied version facts; collection belongs to finite services.
import { Ajv2020 } from "ajv/dist/2020.js";
import { SCHEMA as TASK_SCHEMA } from "../generated/task-contract.ts";
import { SCHEMA } from "../generated/version-lock.ts";
import type { VersionLock } from "../generated/version-lock.ts";
import { validatedSnapshot } from "../context/json-snapshot.ts";

const validator = new Ajv2020({ strict: true, ownProperties: true })
  .addSchema(TASK_SCHEMA).compile<VersionLock>(SCHEMA);

export function validateVersionLock(value: unknown): VersionLock {
  return validatedSnapshot(value, validator, "VersionLock");
}
