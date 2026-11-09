import type { HarnessRunner } from "../types.ts";

export function withPreparedAgent<T>(
  execute: (run: HarnessRunner) => Promise<T>,
): Promise<T>;
