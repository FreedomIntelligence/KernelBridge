/** A process-local client port; v3 framing remains authoritative in protocol.mjs. */
export interface EffectRpc {
  call(method: string, params?: Readonly<Record<string, unknown>>): Promise<Record<string, unknown>>;
}
