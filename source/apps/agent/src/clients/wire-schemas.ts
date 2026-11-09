import { Ajv2020 } from "ajv/dist/2020.js";
import type { ValidateFunction } from "ajv";
import { SCHEMA as RSI } from "../generated/rsi.ts";
import { SCHEMA as TASK } from "../generated/task-contract.ts";
import { SCHEMA as VERSION } from "../generated/version-lock.ts";
import { SCHEMA as OBJECTS } from "../generated/objects.ts";
import { SCHEMA as CONTROL } from "../generated/runtime-control.ts";
import { SCHEMA as RUNTIME } from "../generated/runtime.ts";
import { SCHEMA as CONTEXT } from "../generated/context.ts";
import { SCHEMA as HARNESS } from "../generated/harness.ts";
import { SCHEMA as PAYLOAD } from "../generated/rpc-payload.ts";
import { SCHEMA as KNOWLEDGE } from "../generated/knowledge-runtime.ts";

// These generated schemas reference each other. Register the complete local set
// before compiling; a missing contract must never trigger a network schema load.
const schemas = new Ajv2020({ strict: true, ownProperties: true });
for (const schema of [TASK, VERSION, OBJECTS, CONTROL, RUNTIME, CONTEXT, HARNESS, PAYLOAD, KNOWLEDGE, RSI]) {
  schemas.addSchema(schema);
}

export function wireValidator<T>(schemaId: string, definition: string): ValidateFunction<T> {
  const validate = schemas.getSchema<T>(`${schemaId}#/$defs/${definition}`);
  if (!validate) throw new Error(`Missing local wire schema: ${schemaId} ${definition}`);
  return validate;
}
