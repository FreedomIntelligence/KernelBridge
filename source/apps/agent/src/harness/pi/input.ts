import { getCurrentSystemPrompt, getCurrentTools } from "@earendil-works/pi-ai";
import type { Message, TranscriptContext } from "@earendil-works/pi-ai";
import { SCHEMA } from "../../generated/harness.ts";
import type { HarnessInput } from "../../generated/harness.ts";
import type { JsonObject } from "../../generated/context.ts";
import { validatedSnapshot } from "../../context/json-snapshot.ts";
import { wireValidator } from "../../clients/wire-schemas.ts";

const validate = wireValidator<HarnessInput>(SCHEMA.$id, "HarnessInput");

function content(value: Message["content"]): string | JsonObject[] {
  if (typeof value === "string") return value;
  return value.map((block): JsonObject => {
    if (block.type === "text") return { type: "text", text: block.text };
    if (block.type === "toolCall") {
      return { type: "toolCall", id: block.id, name: block.name, arguments: block.arguments };
    }
    throw new Error(`The controlled text-only Pi transport cannot send ${block.type} content`);
  });
}

function message(value: Message): JsonObject {
  const projected = { role: value.role, content: content(value.content) };
  return value.role === "toolResult" ? { ...projected,
    toolCallId: value.toolCallId, toolName: value.toolName, isError: value.isError,
  } : projected;
}

/** Match Pi's public collapsed-transcript semantics, without its diagnostic metadata. */
export function projectHarness(context: TranscriptContext): HarnessInput {
  const tools = getCurrentTools(context.messages).map((tool) => ({
    name: tool.name,
    description: tool.description,
    // Strip TypeBox's symbol metadata, exactly as Pi's own toToolDeclaration does.
    parameters: JSON.parse(JSON.stringify(tool.parameters)) as JsonObject,
  }));
  const projection = {
    schema_version: "kernelcat.pi-harness/v1", adapter: "pi@1.0.0",
    system_prompt: getCurrentSystemPrompt(context.messages),
    messages: context.messages.filter((item) => item.role !== "system").map(message),
    tools,
  };
  return validatedSnapshot(projection, validate, "Effective Pi input");
}
