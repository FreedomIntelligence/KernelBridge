import { spawnSync } from "node:child_process";
import { constants } from "node:fs";
import { lstat, open, realpath } from "node:fs/promises";
import { basename, dirname, isAbsolute, relative, resolve, sep } from "node:path";

// Only this fixed program runs in the child. Report bytes travel through stdin,
// never through JavaScript source; the child's cwd anchors relative creation.
const writerSource = String.raw`
import { constants, closeSync, fsyncSync, openSync, readFileSync, statSync, writeFileSync } from "node:fs";
const [device, inode, name, maximumBytes] = process.argv.slice(1);
try {
  const directory = statSync(".", { bigint: true });
  if (!directory.isDirectory() || directory.dev.toString() !== device || directory.ino.toString() !== inode) {
    throw Object.assign(new Error("Output directory changed"), { code: "OUTPUT_DIRECTORY_CHANGED" });
  }
  const bytes = readFileSync(0);
  if (bytes.length > Number(maximumBytes)) {
    throw Object.assign(new Error("Output byte bound exceeded"), { code: "OUTPUT_BYTE_BOUND" });
  }
  const file = openSync(name, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | constants.O_NOFOLLOW, 0o600);
  try {
    writeFileSync(file, bytes);
    fsyncSync(file);
  } finally {
    closeSync(file);
  }
} catch (error) {
  const code = typeof error.code === "string" ? error.code : "OUTPUT_WRITE_FAILED";
  process.stdout.write(JSON.stringify({ code }));
  process.exitCode = 1;
}
`;

function contains(root: string, path: string): boolean {
  const part = relative(root, path);
  return part === "" || (part !== ".." && !part.startsWith(`..${sep}`) && !isAbsolute(part));
}

function changedDirectory(): Error {
  return new Error("Output directory changed; requested report path is not confirmed");
}

function runWriter(parent: string, name: string, device: string, inode: string,
  bytes: Buffer, maximumBytes: number): void {
  const result = spawnSync(process.execPath, ["--input-type=module", "--eval", writerSource,
    device, inode, name, String(maximumBytes)], {
    cwd: parent, input: bytes, encoding: "utf8", maxBuffer: 2048, timeout: 30000, killSignal: "SIGKILL",
  });
  if (result.error) throw new Error("Report writer could not complete", { cause: result.error });
  if (result.status === 0) return;
  let code = "OUTPUT_WRITE_FAILED";
  try {
    const failure: unknown = JSON.parse(result.stdout);
    if (failure !== null && typeof failure === "object" && "code" in failure
      && typeof failure.code === "string" && /^[A-Z][A-Z0-9_]{1,63}$/.test(failure.code)) {
      code = failure.code;
    }
  } catch { /* A killed or failed child may have no complete protocol response. */ }
  throw Object.assign(new Error(`Report writer refused output (${code})`), { code });
}

/** Create one bounded report in the admitted inode, without process-wide chdir. */
export async function writeSessionOutput(path: string, bytes: Buffer, maximumBytes: number,
  forbiddenRoots: readonly string[]): Promise<void> {
  if (bytes.length > maximumBytes) throw new Error("Session analysis exceeds the output-byte bound");
  const parent = await realpath(dirname(path));
  const destination = resolve(parent, basename(path));
  const before = await lstat(parent, { bigint: true });
  if (!before.isDirectory() || before.isSymbolicLink()) throw changedDirectory();
  const directory = await open(parent, constants.O_RDONLY | constants.O_DIRECTORY | constants.O_NOFOLLOW);
  try {
    const admitted = await directory.stat({ bigint: true });
    if (!admitted.isDirectory() || admitted.dev !== before.dev || admitted.ino !== before.ino
      || await realpath(parent) !== parent) throw changedDirectory();
    if (forbiddenRoots.some((root) => contains(root, destination))) {
      throw new Error("Output must be outside the source checkout, input directory and recorded model workspaces");
    }
    runWriter(parent, basename(destination), admitted.dev.toString(), admitted.ino.toString(), bytes, maximumBytes);
    const after = await lstat(parent, { bigint: true });
    if (!after.isDirectory() || after.dev !== admitted.dev || after.ino !== admitted.ino
      || await realpath(parent) !== parent || await realpath(dirname(path)) !== parent) throw changedDirectory();
  } finally {
    await directory.close();
  }
}
