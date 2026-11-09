import { createHash } from "node:crypto";
import { mkdir, open, realpath } from "node:fs/promises";
import { dirname, isAbsolute, join } from "node:path";
import { parseSessionEntries, SessionManager } from "@earendil-works/pi-coding-agent";
import type { LoopXSessionFork, NativeCheckpoint, NativeReplay } from "../generated/harness.ts";
import { SCHEMA } from "../generated/harness.ts";
import { wireValidator } from "../clients/wire-schemas.ts";
import { validatedSnapshot } from "../context/json-snapshot.ts";
import { inspectNativeSessionFile } from "../replay/native-session.ts";
import { sessionBytes } from "../replay/product-session.ts";

const digest = (bytes: Buffer) => `sha256:${createHash("sha256").update(bytes).digest("hex")}`;

async function original(replay: NativeReplay): Promise<{ path: string; manager: SessionManager }> {
  const checkpoint = replay.checkpoint;
  if (checkpoint === null) throw new Error("Fork requires an admitted immutable checkpoint");
  const path = join(replay.directory, "checkpoints", `${checkpoint.digest.slice(7)}.jsonl`);
  if (!isAbsolute(path) || await realpath(path) !== path) throw new Error("Fork source must be canonical");
  const bytes = await sessionBytes(path);
  const inspected = await inspectNativeSessionFile(path);
  if (digest(bytes) !== checkpoint.digest || `sha256:${inspected.digest}` !== checkpoint.digest
      || inspected.cwd !== replay.workspace || inspected.sessionId !== checkpoint.session_id
      || inspected.leafId !== checkpoint.leaf_id) throw new Error("Fork source differs from its original checkpoint");
  return { path, manager: SessionManager.inMemory(replay.workspace, undefined, parseSessionEntries(bytes.toString("utf8"))) };
}

async function retainCheckpoint(manager: SessionManager, directory: string): Promise<NativeCheckpoint> {
  const path = manager.getSessionFile();
  const leaf = manager.getLeafId();
  if (!path || leaf === null) throw new Error("Fork has no persistent native boundary");
  const bytes = await sessionBytes(path);
  const inspected = await inspectNativeSessionFile(path);
  if (`sha256:${inspected.digest}` !== digest(bytes) || inspected.leafId !== leaf
      || inspected.sessionId !== manager.getSessionId()) throw new Error("Fork changed before retention");
  const checkpoint: NativeCheckpoint = { session_id: manager.getSessionId(), leaf_id: leaf,
    digest: digest(bytes), workspace_history: ["Workspace history unknown: native fork has not started Pi."] };
  const handle = await open(join(directory, "checkpoints", `${checkpoint.digest.slice(7)}.jsonl`), "wx", 0o400);
  try { await handle.writeFile(bytes); await handle.sync(); }
  finally { await handle.close(); }
  return checkpoint;
}

/** One admitted finite SDK operation. It never prompts, retries or rewrites an original session. */
export async function forkCheckpoint(value: unknown): Promise<NativeCheckpoint> {
  const request = validatedSnapshot(value, wireValidator<LoopXSessionFork>(SCHEMA.$id, "LoopXSessionFork"),
    "LoopX native fork");
  const source = await original(request.source);
  const latest = await original(request.latest);
  if (!isAbsolute(request.directory) || !isAbsolute(request.workspace)
      || await realpath(request.workspace) !== request.workspace
      || await realpath(dirname(request.directory)) !== dirname(request.directory)) {
    throw new Error("Fork requires canonical owner roots");
  }
  await mkdir(request.directory); // Existing attempts must be reconciled by their original owner.
  if (await realpath(request.directory) !== request.directory) throw new Error("Fork directory cannot use aliases");
  for (const name of ["agent", "sessions", "checkpoints", "history"]) await mkdir(join(request.directory, name));
  const manager = SessionManager.forkFrom(source.path, request.workspace, join(request.directory, "sessions"),
    { id: request.session_id });
  const included = new Set<string>();
  if (request.reset_context) manager.resetLeaf();
  else for (const entry of source.manager.getBranch()) included.add(entry.id);
  // Later user constraints survive a historical program rollback. Only real
  // user messages are appended; no assistant call or tool result is invented.
  for (const entry of [...source.manager.getBranch(), ...latest.manager.getBranch()]) {
    if (entry.type === "message" && entry.message.role === "user" && !included.has(entry.id)) {
      manager.appendMessage(entry.message);
      included.add(entry.id);
    }
  }
  // Pi fills missing thinking metadata during import. Retain the current SDK
  // setting now, so that initialization cannot move the admitted fork boundary.
  if (!manager.getBranch().some((entry) => entry.type === "thinking_level_change")) {
    manager.appendThinkingLevelChange(latest.manager.buildSessionContext().thinkingLevel);
  }
  if (manager.getSessionId() !== request.session_id || manager.getCwd() !== request.workspace) {
    throw new Error("Pi fork did not retain the admitted session and workspace");
  }
  await original(request.source);
  await original(request.latest);
  return await retainCheckpoint(manager, request.directory);
}
