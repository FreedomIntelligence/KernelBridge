import assert from 'node:assert/strict';
import { readFile, writeFile } from 'node:fs/promises';
import { codegenSettings, startCodegen } from '../../source/apps/agent/src/application/codegen.mjs';
import { ApplicationSession } from '../../source/apps/agent/src/sdk/application-session.mjs';

const options = JSON.parse(await readFile(process.argv[2], 'utf8'));
const resume = process.argv[3] === '--resume' ? process.argv[4] : null;
const settings = await codegenSettings(options); // Reload private credentials for either path.
const session = resume ? await ApplicationSession.attach(resume, { restart: true })
  : new ApplicationSession(await startCodegen(options, settings, {}));
let pid;
try {
  const before = await session.state(); pid = before.host_pid;
  assert.equal(before.pending_effect_ids.length, 0, 'Recover original effects before sending another input');
  assert.equal(Object.keys(before.calls).length, 0, 'Recover original calls before sending another input');
  await session.prompt(resume ? 'Continue the original workflow after the interrupted observer. Inspect pipeline_status and original receipts. Reuse the completed original source/contract handle even if only the performance baseline changed: comparator changes invalidate comparison/check bindings but do not require another launch. Repeat a check only if its current binding is invalidated. Finish the remaining comparison, promotion and archive within the original local fixture scope, inspect the next-cycle state, then stop. No device or official claim.'
    : options.prompt ?? 'Implement and optimize this local protocol fixture until the first correct positive pair is promoted and archived. Inspect the configured pipeline, use its next actions, diagnose failed checks and regressions, and retain evidence. Then inspect the next cycle state and stop. No official/device claim or publication.');
  const state = await session.run();
  assert.equal(state.budget.limit, null);
  assert.equal(state.pending_effect_ids.length, 0);
  assert.equal(Object.keys(state.calls).length, 0);
  assert.equal(state.goal, null);
  await writeFile(options.output + '/final-state.json', JSON.stringify(state, null, 2) + '\n');
} finally {
  session.dispose();
  if (pid) { try { process.kill(pid, 'SIGTERM'); } catch {} }
}
