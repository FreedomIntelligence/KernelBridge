import type { HarnessOptions } from "../types.ts";
import type { EffectRpc } from "../../clients/rpc.ts";

export interface GovernedDriverOptions extends HarnessOptions {
  interactive?: boolean;
  persistent?: boolean;
  semanticCompaction?: boolean;
  extensionFactories?: readonly unknown[];
  driver?: (governed: any) => Promise<any>;
  onBind?: (session: any, controller: any) => Promise<void>;
}

export function withPreparedAgent<T>(
  execute: (run: (client: EffectRpc, options?: GovernedDriverOptions) => Promise<Record<string, unknown>>) => Promise<T>,
): Promise<T>;
