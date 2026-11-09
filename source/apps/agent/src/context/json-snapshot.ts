// Shared detached JSON validation at generated wire boundaries.
import type { ValidateFunction } from "ajv";

function checkJson(value: unknown, label: string, ancestors = new Set<object>()): void {
  if (value === null || typeof value === "string" || typeof value === "boolean") return;
  if (typeof value === "number" && Number.isFinite(value)) return;
  if (typeof value !== "object" || ancestors.has(value)) {
    throw new Error(`${label} must contain finite, acyclic JSON values`);
  }
  const prototype = Object.getPrototypeOf(value);
  if (!Array.isArray(value) && prototype !== Object.prototype && prototype !== null) {
    throw new Error(`${label} objects must be JSON records`);
  }
  if (Reflect.ownKeys(value).some((key) => typeof key !== "string")) {
    throw new Error(`${label} object keys must be strings`);
  }
  ancestors.add(value);
  for (const item of Object.values(value)) checkJson(item, label, ancestors);
  ancestors.delete(value);
}

export function validatedSnapshot<T>(value: unknown, validator: ValidateFunction<T>, label: string): T {
  let snapshot: unknown;
  try {
    snapshot = structuredClone(value);
  } catch (error) {
    throw new Error(`${label} must contain cloneable JSON values`, { cause: error });
  }
  // Cloning can invoke accessors; only the detached tree is the validated value.
  checkJson(snapshot, label);
  if (!validator(snapshot)) {
    const error = validator.errors?.[0];
    throw new Error(`Invalid ${label} at ${error?.instancePath || "/"}: ${error?.message}`);
  }
  return snapshot;
}
