import type { EffectRpc } from "./rpc.ts";

/** One ordered wire lane. A failed exchange poisons queued work instead of retrying it. */
export function serialRpc(client: EffectRpc): EffectRpc {
  let tail: Promise<unknown> = Promise.resolve();
  let failure: unknown;
  let failed = false;
  return {
    call(method, params) {
      const operation = tail.then(async () => {
        if (failed) throw failure;
        try {
          return await client.call(method, params);
        } catch (error) {
          failed = true;
          failure = error;
          throw error;
        }
      });
      tail = operation.catch(() => undefined);
      return operation;
    },
  };
}
