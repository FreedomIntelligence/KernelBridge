// Finite declared-policy boundary; this grants no filesystem or network permission.
import { Ajv2020 } from "ajv/dist/2020.js";
import { SCHEMA as KNOWLEDGE_SCHEMA } from "../generated/knowledge-runtime.ts";
import { SCHEMA as OBJECT_SCHEMA } from "../generated/objects.ts";
import { SCHEMA as TASK_SCHEMA } from "../generated/task-contract.ts";
import { SCHEMA } from "../generated/trust-boundary.ts";
import type { TrustBoundaryDeclaration } from "../generated/trust-boundary.ts";
import { SCHEMA as VERSION_SCHEMA } from "../generated/version-lock.ts";
import { validatedSnapshot } from "./json-snapshot.ts";

const validator = new Ajv2020({ strict: true, ownProperties: true })
  .addSchema(KNOWLEDGE_SCHEMA).addSchema(TASK_SCHEMA).addSchema(VERSION_SCHEMA).addSchema(OBJECT_SCHEMA)
  .compile<TrustBoundaryDeclaration>(SCHEMA);

export function validateTrustBoundaryDeclaration(value: unknown): TrustBoundaryDeclaration {
  return validatedSnapshot(value, validator, "TrustBoundaryDeclaration");
}
