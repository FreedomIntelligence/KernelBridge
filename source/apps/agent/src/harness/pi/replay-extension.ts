import type {
  AgentToolResult, ExtensionContext, ExtensionFactory, ToolDefinition,
} from "@earendil-works/pi-coding-agent";
import { SCHEMA } from "../../generated/rsi.ts";
import type { ReplayPluginConfiguration, ReplayToolArguments } from "../../generated/rsi.ts";
import { validatedSnapshot } from "../../context/json-snapshot.ts";
import { wireValidator } from "../../clients/wire-schemas.ts";

export const REPLAY_TOOL = "replay";

interface ReplayController {
  fail(error: unknown): void;
  tool(id: string, name: string, arguments_: unknown, signal?: AbortSignal): Promise<AgentToolResult<unknown>>;
}

type ToolSpecification = Pick<ToolDefinition, "name" | "description" | "parameters">;

interface ReplayBinding {
  readonly configuration: unknown;
  readonly native: boolean;
  readonly tools: readonly ToolSpecification[];
  readonly controller: ReplayController;
}

export interface NativeReplayPlugin {
  readonly configuration: ReplayPluginConfiguration;
  readonly factory: ExtensionFactory;
}

function configuration(value: unknown): ReplayPluginConfiguration | undefined {
  if (value === undefined) {
    return undefined;
  }
  return validatedSnapshot(value,
    wireValidator<ReplayPluginConfiguration>(SCHEMA.$id, "ReplayPluginConfiguration"), "Replay plugin configuration");
}

async function request(binding: ReplayBinding, context: ExtensionContext, id: string,
  arguments_: unknown, signal?: AbortSignal): Promise<AgentToolResult<unknown>> {
  try {
    const argumentsSnapshot = validatedSnapshot(arguments_,
      wireValidator<ReplayToolArguments>(SCHEMA.$id, "ReplayToolArguments"), "Replay tool arguments");
    const result = await binding.controller.tool(id, REPLAY_TOOL, argumentsSnapshot, signal);
    if (result.isError) {
      binding.controller.fail(new Error(`Replay request ${id} failed; inspect its original Gateway outcome`));
      context.abort();
    }
    return result;
  } catch (error) {
    binding.controller.fail(error);
    context.abort();
    throw error;
  }
}

/** Native lifecycle only; the sealed Gateway records requests and the parent restores them later. */
export function createReplayExtension(binding: ReplayBinding): NativeReplayPlugin | null {
  const selected = configuration(binding.configuration);
  const declarations = binding.tools.filter((tool) => tool.name === REPLAY_TOOL);
  if (!selected?.enabled) {
    if (declarations.length) {
      throw new Error("Disabled replay plugin cannot expose a replay tool");
    }
    return null;
  }
  if (!binding.native) {
    throw new Error("Replay plugin requires native Pi Harness");
  }
  if (declarations.length !== 1 || typeof declarations[0].description !== "string"
      || declarations[0].parameters === null || typeof declarations[0].parameters !== "object"
      || Array.isArray(declarations[0].parameters)) {
    throw new Error("Replay plugin requires one Host-declared replay tool");
  }
  const tool = declarations[0];
  return { configuration: selected, factory: (pi) => {
    pi.registerTool({ ...tool, label: REPLAY_TOOL, exposure: "model-only",
      execute: (id, arguments_, signal, _onUpdate, context) => request(binding, context, id, arguments_, signal) });
  } };
}
