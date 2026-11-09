// Explicit file-to-report boundary. This command never executes selected checks.
import { constants, closeSync, fstatSync, openSync, readSync } from "node:fs";
import { planVersionTests } from "../src/replay/version-tests.ts";

const MAX_INPUT_BYTES = 1024 * 1024;
const USAGE = "Usage: version-test-plan --current FILE [--previous FILE]";

interface InputPaths {
  current: string;
  previous?: string;
}

function parseArguments(argv: readonly string[]): InputPaths {
  const values = new Map<string, string>();
  for (let index = 0; index < argv.length; index += 2) {
    const option = argv[index];
    if (option !== "--current" && option !== "--previous") {
      throw new Error(`Unknown option ${JSON.stringify(option)}. ${USAGE}`);
    }
    if (values.has(option)) throw new Error(`Duplicate option ${option}. ${USAGE}`);
    const path = argv[index + 1];
    if (!path || path.startsWith("-")) throw new Error(`Missing explicit file path for ${option}. ${USAGE}`);
    values.set(option, path);
  }
  const current = values.get("--current");
  if (!current) throw new Error(`Required option --current is missing. ${USAGE}`);
  return { current, previous: values.get("--previous") };
}

function readBoundedFile(path: string): string {
  // Nonblocking open allows a named pipe to be rejected as non-regular, without
  // waiting for another process. A growing regular file still has a hard read cap.
  const file = openSync(path, constants.O_RDONLY | constants.O_NONBLOCK);
  try {
    const metadata = fstatSync(file);
    if (!metadata.isFile()) throw new Error("input must be a regular JSON file");
    if (metadata.size > MAX_INPUT_BYTES) throw new Error(`input exceeds ${MAX_INPUT_BYTES} bytes`);
    const bytes = Buffer.alloc(MAX_INPUT_BYTES + 1);
    let length = 0;
    while (length < bytes.length) {
      const count = readSync(file, bytes, length, bytes.length - length, null);
      if (count === 0) break;
      length += count;
    }
    if (length > MAX_INPUT_BYTES) throw new Error(`input exceeds ${MAX_INPUT_BYTES} bytes`);
    return new TextDecoder("utf-8", { fatal: true }).decode(bytes.subarray(0, length));
  } finally {
    closeSync(file);
  }
}

function readInput(path: string, role: "current" | "previous"): unknown {
  try {
    return JSON.parse(readBoundedFile(path));
  } catch (error) {
    throw new Error(`Cannot read ${role} VersionLock ${JSON.stringify(path)}: ${String(error)}`, { cause: error });
  }
}

function main(argv: readonly string[]): void {
  const paths = parseArguments(argv);
  const current = readInput(paths.current, "current");
  const previous = paths.previous === undefined ? undefined : readInput(paths.previous, "previous");
  const plan = planVersionTests(current, previous);
  process.stdout.write(`${JSON.stringify(plan, null, 2)}\n`);
}

try {
  main(process.argv.slice(2));
} catch (error) {
  process.stderr.write(`version-test-plan: ${error instanceof Error ? error.message : String(error)}\n`);
  process.exitCode = 1;
}
