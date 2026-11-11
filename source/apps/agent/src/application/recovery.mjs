import { createHash, randomUUID } from 'node:crypto';
import { mkdir, open, readFile, writeFile } from 'node:fs/promises';
import { join } from 'node:path';

/** Reconstruct a conversation boundary from original receipts without model/tool dispatch. */
export async function recoverApplication(lease) {
  const plan = await lease.call('application_recovery_plan');
  if (!plan.turns.length) {
    await lease.call('application_recovery_commit', { plan_digest: plan.plan_digest });
    return;
  }
  const { SessionManager } = await import('@earendil-works/pi-coding-agent');
  const directory = join(plan.directory, 'sessions');
  await mkdir(directory, { recursive: true, mode: 0o700 });
  let manager;
  if (plan.checkpoint?.session_file) {
    let original;
    try { original = await readFile(plan.checkpoint.session_file); }
    catch (error) {
      if (error.code !== 'ENOENT' || !plan.checkpoint.native_checkpoint) throw error;
      original = await readFile(join(plan.directory, 'checkpoints', plan.checkpoint.native_checkpoint.digest.slice(7) + '.jsonl'));
    }
    if (original.length > 32 * 1024 * 1024) throw new Error('Recovery session exceeds 32 MiB');
    // Preserve the original file and recover only its last complete JSONL prefix.
    const end = original.lastIndexOf(10);
    if (end < 0) throw new Error('Original session has no complete JSONL boundary');
    const prefix = original.subarray(0, end + 1);
    for (const line of prefix.toString('utf8').trimEnd().split('\n')) JSON.parse(line);
    const staged = join(directory, `recovery-${randomUUID()}.jsonl`);
    await writeFile(staged, prefix, { flag: 'wx', mode: 0o600 });
    manager = SessionManager.open(staged, directory);
    if (manager.getCwd() !== plan.workspace) throw new Error('Recovery workspace mismatch');
  } else {
    throw new Error('A model turn has no retained native session file; retain receipts for explicit reconciliation');
  }
  for (const turn of plan.turns) {
    for (const item of turn.calls) {
      const entries = manager.getBranch();
      const assistant = entries.find(entry => entry.type === 'message' && entry.message.role === 'assistant'
        && entry.message.content.some(block => block.type === 'toolCall' && block.id === item.tool_call_id));
      const result = entries.find(entry => entry.type === 'message' && entry.message.role === 'toolResult'
        && entry.message.toolCallId === item.tool_call_id);
      if (assistant && !result) {
        manager.appendMessage({ role: 'toolResult', toolCallId: item.tool_call_id, toolName: item.tool_name,
          content: [{ type: 'text', text: JSON.stringify({ action_id: item.action_id, outcome: item.outcome, recovered: true }) }],
          isError: item.outcome.status !== 'completed', timestamp: Date.now() });
      } else if (!assistant) {
        manager.appendCustomMessageEntry('kernelbridge-recovered', JSON.stringify({ recovered: true,
          original_tool_call: item.tool_call_id, arguments: item.arguments,
          action_id: item.action_id, original_receipt: item.outcome }), true);
      }
    }
    manager.appendCustomMessageEntry('kernelbridge-recovery-boundary', JSON.stringify({ recovered: true,
      proposal_id: turn.proposal_id, model_receipt: turn.model_receipt,
      note: 'Original model receipt retained; unfinished calls were reconciled without dispatch.' }), true);
  }
  const bytes = await readFile(manager.getSessionFile());
  const digest = createHash('sha256').update(bytes).digest('hex');
  const checkpoints = join(plan.directory, 'checkpoints');
  await mkdir(checkpoints, { recursive: true, mode: 0o700 });
  const file = await open(join(checkpoints, `${digest}.jsonl`), 'wx', 0o400);
  try { await file.writeFile(bytes); await file.sync(); } finally { await file.close(); }
  const parent = await open(checkpoints, 'r');
  try { await parent.sync(); } finally { await parent.close(); }
  await lease.call('application_recovery_commit', { plan_digest: plan.plan_digest,
    checkpoint: { session_id: manager.getSessionId(), leaf_id: manager.getLeafId(),
      digest: `sha256:${digest}`, workspace_history: ['Recovered from original Journal receipts; no tool redispatch'] } });
}
