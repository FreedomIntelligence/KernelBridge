import type { EffectRpc } from "../clients/rpc.ts";

export interface HarnessEvent {
  readonly type: string;
  readonly [key: string]: unknown;
}

export interface HarnessOptions {
  readonly signal?: AbortSignal;
  readonly onEvent?: (event: HarnessEvent) => void;
}

/** A Harness advances assistant turns using only the supplied finite effect port. */
export type HarnessRunner = (rpc: EffectRpc, options?: HarnessOptions) => Promise<Record<string, unknown>>;
