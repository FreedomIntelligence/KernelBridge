import type {
  AgentToolResult, ExtensionContext, ExtensionFactory, ToolDefinition,
} from "@earendil-works/pi-coding-agent";
import type { EffectRpc } from "../../clients/rpc.ts";

export const RSI_TOOL = "run_experiment";

interface RsiController {
  failure: Error | null;
  active: object | null;
  stopped: boolean;
  fail(error: unknown): void;
  tool(id: string, name: string, arguments_: unknown, signal?: AbortSignal): Promise<AgentToolResult<unknown>>;
}

type ToolSpecification = Pick<ToolDefinition, "name" | "description" | "parameters">;

interface RsiBinding {
  capability: unknown;
  native: boolean;
  tools: readonly ToolSpecification[];
  rpc: EffectRpc;
  controller: RsiController;
}

function experimentTool(binding: RsiBinding): ToolSpecification | null {
  if (binding.capability === undefined) {
    return null;
  }
  const capability = binding.capability;
  if (!binding.native || capability === null || typeof capability !== "object"
      || Array.isArray(capability) || Object.keys(capability).join(",") !== "schema_version"
      || !("schema_version" in capability) || capability.schema_version !== "kernelcat.rsi/v1") {
    throw new Error("Unsupported RSI capability; native Pi Harness is required");
  }
  const tools = binding.tools.filter((tool) => tool.name === RSI_TOOL);
  if (tools.length !== 1 || typeof tools[0].description !== "string"
      || tools[0].parameters === null || typeof tools[0].parameters !== "object"
      || Array.isArray(tools[0].parameters)) {
    throw new Error("RSI requires one Host-declared run_experiment tool");
  }
  return tools[0];
}

async function guarded<T>(controller: RsiController, context: ExtensionContext, effect: () => Promise<T>): Promise<T> {
  try {
    return await effect();
  } catch (error) {
    // Pi reports hook errors and continues. Both the abort and the controlled
    // provider's failure check are required to prevent an unprepared model call.
    controller.fail(error);
    context.abort();
    throw error;
  }
}

async function prepare(binding: RsiBinding, context: ExtensionContext): Promise<void> {
  const { controller, rpc } = binding;
  if (controller.failure || controller.stopped) {
    context.abort();
    return;
  }
  const prepared = await rpc.call("rsi_prepare", {});
  if (Object.keys(prepared).join(",") !== "stop" || typeof prepared.stop !== "boolean") {
    throw new Error("Invalid RSI preparation response");
  }
  if (prepared.stop) {
    controller.stopped = true;
    context.abort();
    return;
  }
}

async function settle(rpc: EffectRpc): Promise<unknown> {
  const response = await rpc.call("rsi_settle", {});
  if (Object.keys(response).join(",") !== "result") throw new Error("Invalid RSI settlement response");
  return response.result === null ? undefined : response.result;
}

/** Explicit parent-session factory. The original Journal remains the state owner. */
export function createRsiExtension(binding: RsiBinding): ExtensionFactory | null {
  const tool = experimentTool(binding);
  if (tool === null) {
    return null;
  }
  const { controller, rpc } = binding;
  return (pi) => {
    pi.registerTool({
      ...tool, label: RSI_TOOL, exposure: "model-only",
      execute: (id, arguments_, signal, _onUpdate, context) => guarded(controller, context,
        () => controller.tool(id, RSI_TOOL, arguments_, signal)),
    });
    pi.on("context", (_event, context) => guarded(controller, context, () => prepare(binding, context)));
    pi.on("turn_end", (event, context) => {
      if (controller.failure || controller.stopped || controller.active === null || event.outcome !== "completed") {
        return;
      }
      return guarded(controller, context, async () => {
        const result = await settle(rpc);
        if (result !== undefined) {
          pi.sendMessage({ customType: "kernelcat.rsi-result", content: JSON.stringify(result), display: false },
            { triggerTurn: false });
        }
      });
    });
  };
}
