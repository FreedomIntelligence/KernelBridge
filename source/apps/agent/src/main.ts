import { pathToFileURL } from "node:url";
import { canonicalKey } from "./run/scoring/canonical.ts";
import { HELP, parseArguments } from "./cli/arguments.ts";
import { runProduct } from "./bootstrap.ts";

const legacyCommands = new Set(['run', 'pursue', 'loopx-cycle', 'rsi-collect', 'rsi-replay', 'account-create', 'runtime-state', 'stop-run', 'knowledge-fence', 'knowledge-sync']);

export async function main(args: readonly string[]): Promise<number> {
  try {
    if (!legacyCommands.has(args[0])) {
      const { applicationMain } = await import("./application/cli.mjs");
      return await applicationMain(args.length === 0 ? ['tui'] : args);
    }
    const options = parseArguments(args);
    if (options === null) {
      process.stdout.write(HELP);
      return 0;
    }
    const result = await runProduct(options);
    process.stdout.write(canonicalKey(result) + "\n");
    const success = options.command === "pursue"
      ? ["completed", "paused", "invocation_limit"].includes(String(result.status))
      : options.command === "loopx-cycle"
      ? ["ready", "completed", "turn_limit"].includes(String(result.status))
      : options.command === "rsi-collect" ? result.status === "completed"
      : options.command === "rsi-replay" ? ["disabled", "no_request", "restored"].includes(String(result.status))
      : result.status === "finished" || result.status === "paused" && result.replay !== undefined;
    return success ? 0 : 1;
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    if (legacyCommands.has(args[0])) process.stdout.write(canonicalKey({ status: "error", error: message }) + "\n");
    process.stderr.write(message + "\n");
    return 1;
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  process.exitCode = await main(process.argv.slice(2));
}
