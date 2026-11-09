// Finite protocol boundary; the TS planner separately owns transition policy.
import { Ajv2020 } from "ajv/dist/2020.js";
import { SCHEMA as KNOWLEDGE_SCHEMA } from "../generated/knowledge-runtime.ts";
import { SCHEMA } from "../generated/control.ts";
import type { ControlMessage } from "../generated/control.ts";
import { SCHEMA as OBJECT_SCHEMA } from "../generated/objects.ts";
import { SCHEMA as TASK_SCHEMA } from "../generated/task-contract.ts";
import { SCHEMA as VERSION_SCHEMA } from "../generated/version-lock.ts";
import { validatedSnapshot } from "../context/json-snapshot.ts";

const validator = new Ajv2020({ strict: true, ownProperties: true })
  .addSchema(KNOWLEDGE_SCHEMA).addSchema(TASK_SCHEMA).addSchema(VERSION_SCHEMA).addSchema(OBJECT_SCHEMA)
  .compile<ControlMessage>(SCHEMA);

export function validateControlMessage(value: unknown): ControlMessage {
  return validatedSnapshot(value, validator, "ControlMessage");
}
