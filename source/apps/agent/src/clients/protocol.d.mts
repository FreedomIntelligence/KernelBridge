import type { Readable, Writable } from "node:stream";
import type { EffectRpc } from "./rpc.ts";

export const PROTOCOL: "kernelcat.pi-worker/v3";
export const DEFAULT_FRAME_BYTES: number;
export function frames(input: Readable, limit?: number): AsyncGenerator<unknown>;
export class RpcClient implements EffectRpc {
  constructor(input: Readable, output: Writable, limit?: number, maxPayloadBytes?: number);
  call(method: string, params?: Readonly<Record<string, unknown>>): Promise<Record<string, unknown>>;
}
