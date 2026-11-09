import { createHash } from "node:crypto";
import { mkdir, open, readFile, unlink } from "node:fs/promises";
import { join } from "node:path";

/** Persistent identity fence only. Goal state, usage and results remain in LoopX. */
export async function claimGoalOwner(runtimeRoot, goalId, binding) {
  const directory = join(runtimeRoot, "kernelcat-bindings");
  await mkdir(directory, { recursive: true, mode: 0o700 });
  const key = createHash("sha256").update(goalId).digest("hex");
  const path = join(directory, `${key}.json`);
  const encoded = JSON.stringify(binding);
  let file;
  try { file = await open(path, "wx", 0o600); }
  catch (error) {
    if (error.code !== "EEXIST") throw error;
    if (await readFile(path, "utf8") !== encoded) throw new Error("Goal has a different continuation owner or binding");
    return directory;
  }
  try { await file.writeFile(encoded); await file.sync(); }
  finally { await file.close(); }
  const parent = await open(directory, "r");
  try { await parent.sync(); } finally { await parent.close(); }
  return directory;
}

function applicationPath(runtimeRoot, applicationSessionId) {
  return join(runtimeRoot, "kernelcat-bindings", `application-${createHash("sha256").update(applicationSessionId).digest("hex")}.json`);
}
async function applicationMutation(path, action) {
  const lock = await open(`${path}.lock`, "wx", 0o600);
  try { return await action(); }
  finally { await lock.close(); await unlink(`${path}.lock`); }
}
export async function claimApplicationGoal(runtimeRoot, applicationSessionId, goalId) {
  const path = applicationPath(runtimeRoot, applicationSessionId);
  await applicationMutation(path, async () => {
    const identity = JSON.stringify({ applicationSessionId, goalId });
    let file;
    try { file = await open(path, "wx", 0o600); }
    catch (error) {
      if (error.code !== "EEXIST") throw error;
      await assertApplicationGoal(runtimeRoot, applicationSessionId, goalId);
      return;
    }
    try { await file.writeFile(identity); await file.sync(); } finally { await file.close(); }
  });
}
export async function assertApplicationGoal(runtimeRoot, applicationSessionId, goalId) {
  const value = await readFile(applicationPath(runtimeRoot, applicationSessionId), "utf8");
  if (value !== JSON.stringify({ applicationSessionId, goalId })) throw new Error("Application session has a different Goal binding");
}
export async function clearApplicationGoal(runtimeRoot, applicationSessionId, goalId) {
  const path = applicationPath(runtimeRoot, applicationSessionId);
  await applicationMutation(path, async () => {
    await assertApplicationGoal(runtimeRoot, applicationSessionId, goalId);
    await unlink(path);
  });
}
