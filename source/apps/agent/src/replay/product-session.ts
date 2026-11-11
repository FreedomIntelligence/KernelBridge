import { createHash, randomUUID } from "node:crypto";
import { constants } from "node:fs";
import { open, readFile, realpath, writeFile } from "node:fs/promises";
import { dirname, isAbsolute, join } from "node:path";
import { fileURLToPath } from "node:url";
import type { AgentSession, AgentSessionRuntime } from "@earendil-works/pi-coding-agent";
import { SCHEMA } from "../generated/harness.ts";
import type { NativeCheckpoint, NativeReplay } from "../generated/harness.ts";
import { validatedSnapshot } from "../context/json-snapshot.ts";
import { wireValidator } from "../clients/wire-schemas.ts";
import { observeWorkspaceHistory, prepareWorkspaceHistory } from "../harness/pi/workspace-history.ts";
import { inspectNativeSessionFile } from "./native-session.ts";

const MAX_SESSION_BYTES = 32 * 1024 * 1024;
const digest = (bytes: Buffer) => `sha256:${createHash("sha256").update(bytes).digest("hex")}`;

export async function sessionBytes(path: string): Promise<Buffer> {
  const handle = await open(path, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
  try {
    const info = await handle.stat();
    if (!info.isFile() || info.size <= 0 || info.size > MAX_SESSION_BYTES) {
      throw new Error("Native Pi session must be a nonempty regular file of at most 32 MiB");
    }
    const bytes = Buffer.alloc(MAX_SESSION_BYTES + 1);
    let size = 0;
    while (size < bytes.length) {
      const result = await handle.read(bytes, size, bytes.length - size, null);
      if (result.bytesRead === 0) break;
      size += result.bytesRead;
    }
    if (size > MAX_SESSION_BYTES) throw new Error("Native Pi session grew beyond 32 MiB");
    return bytes.subarray(0, size);
  } finally {
    await handle.close();
  }
}

export async function prepareProductReplay(value: unknown, { project = false } = {}) {
  const capability = validatedSnapshot(value, wireValidator<NativeReplay>(SCHEMA.$id, "NativeReplay"), "Native replay");
  for (const path of [capability.directory, capability.workspace]) {
    if (!isAbsolute(path) || await realpath(path) !== path) throw new Error("Native replay requires canonical absolute roots");
  }
  const agentDir = join(capability.directory, "agent");
  const history = project ? undefined : await prepareWorkspaceHistory({ workspace: capability.workspace, agentDir,
    storageDir: join(capability.directory, "history"),
    packageDir: dirname(fileURLToPath(import.meta.resolve("pi-workspace-history/package.json"))) });
  return { capability, history, agentDir, sessions: join(capability.directory, "sessions") };
}

export type ProductReplay = Awaited<ReturnType<typeof prepareProductReplay>>;

function checkpointPath(replay: ProductReplay, checkpoint: NativeCheckpoint): string {
  return join(replay.capability.directory, "checkpoints", `${checkpoint.digest.slice(7)}.jsonl`);
}

/** Original Worker hello already admitted this exact original-Journal checkpoint. */
export async function restoreProductSession(runtime: AgentSessionRuntime, replay: ProductReplay): Promise<boolean> {
  const checkpoint = replay.capability.checkpoint;
  if (checkpoint === null) return false;
  const path = checkpointPath(replay, checkpoint);
  const bytes = await sessionBytes(path);
  if (digest(bytes) !== checkpoint.digest) throw new Error("Native checkpoint changed after owner admission");
  const original = await inspectNativeSessionFile(path);
  if (original.sessionId !== checkpoint.session_id || original.leafId !== checkpoint.leaf_id
      || original.cwd !== replay.capability.workspace || `sha256:${original.digest}` !== checkpoint.digest) {
    throw new Error("Native checkpoint does not contain its admitted complete session boundary");
  }
  // Pi import preserves source permissions. Stage an exact writable byte copy;
  // the original immutable checkpoint remains read-only and is never opened by Pi.
  const staged = join(replay.sessions, `${randomUUID()}.jsonl`);
  await writeFile(staged, bytes, { flag: "wx", mode: 0o600 });
  const result = await runtime.importFromJsonl(staged);
  if (result.cancelled) throw new Error("Native checkpoint restore was cancelled");
  const manager = runtime.session.sessionManager;
  if (manager.getSessionId() !== checkpoint.session_id || manager.getLeafId() !== checkpoint.leaf_id
      || manager.getCwd() !== replay.capability.workspace) {
    throw new Error("Restored native session differs from its admitted identity/leaf/workspace");
  }
  return true;
}

/** Freeze the entire original JSONL tree before the existing end_turn admission. */
export async function snapshotProductSession(session: AgentSession, replay: ProductReplay): Promise<NativeCheckpoint> {
  const file = session.sessionFile;
  const leaf = session.sessionManager.getLeafId();
  if (!file || leaf === null) throw new Error("Native turn has no persistent session/leaf");
  const workspaceHistory = replay.history ? [...await observeWorkspaceHistory(session)]
    : ['Project session: conversation checkpoint only; working files are not snapshotted or restored.'];
  let bytes: Buffer;
  try {
    bytes = await sessionBytes(file);
    const original = await inspectNativeSessionFile(file);
    if (original.sessionId !== session.sessionId || original.leafId !== leaf
        || original.cwd !== replay.capability.workspace || `sha256:${original.digest}` !== digest(bytes)) {
      throw new Error("Native session file changed before checkpoint admission");
    }
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
    // Pi defers its mutable JSONL until a user/assistant message. Persist real
    // setup/custom entries through its public tree API without changing Pi's file.
    bytes = Buffer.from([session.sessionManager.getHeader(), ...session.sessionManager.getEntries()]
      .map(entry => JSON.stringify(entry)).join("\n") + "\n");
    if (bytes.length > MAX_SESSION_BYTES) throw new Error("Native checkpoint exceeds 32 MiB");
  }
  const checkpoint: NativeCheckpoint = { session_id: session.sessionId, leaf_id: leaf,
    digest: digest(bytes), workspace_history: workspaceHistory };
  const path = checkpointPath(replay, checkpoint);
  let handle;
  try {
    handle = await open(path, "wx", 0o400);
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "EEXIST") throw error;
    if (!(await readFile(path)).equals(bytes)) throw new Error("Existing immutable Pi checkpoint has different bytes", { cause: error });
  }
  if (handle) {
    try { await handle.writeFile(bytes); await handle.sync(); }
    finally { await handle.close(); }
    const directory = await open(dirname(path), "r");
    try { await directory.sync(); } finally { await directory.close(); }
  }
  return validatedSnapshot(checkpoint, wireValidator<NativeCheckpoint>(SCHEMA.$id, "NativeCheckpoint"), "Native checkpoint");
}
