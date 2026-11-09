// Structural validation is separate from Controller-owned graph coherence.
import { Ajv2020 } from "ajv/dist/2020.js";
import { SCHEMA as KNOWLEDGE_SCHEMA } from "../generated/knowledge-runtime.ts";
import { SCHEMA } from "../generated/objects.ts";
import type { ObjectGraph } from "../generated/objects.ts";
import { SCHEMA as TASK_SCHEMA } from "../generated/task-contract.ts";
import { SCHEMA as VERSION_SCHEMA } from "../generated/version-lock.ts";
import { validatedSnapshot } from "../context/json-snapshot.ts";

const validator = new Ajv2020({ strict: true, ownProperties: true })
  .addSchema(KNOWLEDGE_SCHEMA).addSchema(TASK_SCHEMA).addSchema(VERSION_SCHEMA).compile<ObjectGraph>(SCHEMA);

export function validateObjectGraph(value: unknown): ObjectGraph {
  return validatedSnapshot(value, validator, "ObjectGraph");
}
