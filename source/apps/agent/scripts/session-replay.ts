import { importSession } from "../src/replay/session-import.ts";
import type { SessionImportOptions } from "../src/replay/session-import.ts";

const usage = "Usage: node scripts/session-replay.ts --input ABS --output ABS "
  + "[--prefix-line N] [--divergence-line N] [--artifacts ABS]";

function parseArguments(arguments_: string[]): SessionImportOptions {
  const allowed = new Set(["--input", "--output", "--prefix-line", "--divergence-line", "--artifacts"]);
  const values = new Map<string, string>();
  for (let index = 0; index < arguments_.length; index += 2) {
    const key = arguments_[index]!;
    const value = arguments_[index + 1];
    if (!allowed.has(key) || values.has(key) || value === undefined || value.startsWith("--")) {
      throw new Error("Unknown, duplicate or incomplete session-replay option");
    }
    values.set(key, value);
  }
  if (!values.has("--input") || !values.has("--output")) throw new Error("Input and output are required");
  const line = (key: string): number | undefined => {
    const value = values.get(key);
    if (value === undefined) return undefined;
    if (!/^[1-9][0-9]*$/.test(value) || !Number.isSafeInteger(Number(value))) {
      throw new Error(`${key} requires a positive safe integer`);
    }
    return Number(value);
  };
  return { input: values.get("--input")!, output: values.get("--output")!,
    prefixLine: line("--prefix-line"), divergenceLine: line("--divergence-line"), artifactManifest: values.get("--artifacts") };
}

if (process.argv.slice(2).length === 1 && process.argv[2] === "--help") {
  console.log(usage);
} else {
  try {
    const summary = await importSession(parseArguments(process.argv.slice(2)));
    console.log(JSON.stringify({ authority: summary.authority, source_sha256: summary.source.sha256,
      effective_last_line: summary.replay.effective_last_line, counts: summary.counts,
      pending_calls: summary.current_view.pending_call_ids.length,
      pending_handles: summary.current_view.pending_handles.length,
      net_performance_measured: false, causal_effect_measured: false }));
  } catch (error) {
    console.error(`session-replay: ${error instanceof Error ? error.message : "Import failed"}`);
    process.exitCode = 1;
  }
}
