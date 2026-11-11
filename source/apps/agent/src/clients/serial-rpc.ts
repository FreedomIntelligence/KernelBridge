import type { EffectRpc } from "./rpc.ts";

/** Child-scoped methods that carry their delegation permit. */
const CHILD_METHODS = new Set(["child_model_turn", "child_tool", "plugin_settle"]);

export interface SerialRpcOptions {
  /**
   * Give each delegation permit its own ordered lane. Parent calls keep one
   * lane; a child's model/read/settle calls stay ordered within that child,
   * while different children may be in flight together. Only hosts that
   * execute child model calls concurrently should enable this.
   */
  readonly childLanes?: boolean;
}

/** Ordered wire lanes. A failed exchange poisons all queued work instead of retrying it. */
export function serialRpc(client: EffectRpc, options: SerialRpcOptions = {}): EffectRpc {
  const tails = new Map<string, Promise<unknown>>();
  let failure: unknown;
  let failed = false;
  return {
    call(method, params) {
      const permit = options.childLanes === true && CHILD_METHODS.has(method) && typeof params?.permit === "string"
        ? params.permit : undefined;
      const lane = permit === undefined ? "parent" : `child:${permit}`;
      const operation = (tails.get(lane) ?? Promise.resolve()).then(async () => {
        if (failed) throw failure;
        try {
          return await client.call(method, params);
        } catch (error) {
          failed = true;
          failure = error;
          throw error;
        }
      });
      const tail = operation.catch(() => undefined);
      tails.set(lane, tail);
      if (permit !== undefined && method === "plugin_settle") {
        // A settled permit never issues more calls; release its lane.
        void tail.then(() => { if (tails.get(lane) === tail) tails.delete(lane); });
      }
      return operation;
    },
  };
}
