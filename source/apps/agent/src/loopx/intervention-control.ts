import type { EffectRpc } from "../clients/rpc.ts";
import type { ReplayRequest, RsiBranchRequest } from "../generated/rsi.ts";
import { readRsi, Records } from "../rsi/records.ts";

/** Agent-selected restoration; diagnostics alone never authorize moving code. */
export function selectIntervention(records: Records): RsiBranchRequest | null {
  const requested = records.last("replay_request");
  const previous = records.last("branch");
  if (!requested || previous && previous.sequence >= requested.sequence) return null;
  const original = readRsi<ReplayRequest>(requested.value, "ReplayRequest");
  if (original.action_id !== requested.record_id || original.arguments.mode === "list") {
    throw new Error("Replay restoration lacks an original Agent request");
  }
  return readRsi<RsiBranchRequest>({ decision_id: requested.record_id,
    mode: original.arguments.mode, target_node_id: original.arguments.target_node_id,
    expected_branch_id: previous?.record_id ?? null, expected_sequence: records.sequence }, "RsiBranchRequest");
}

export async function applyIntervention(rpc: EffectRpc): Promise<void> {
  const records = new Records(rpc);
  await records.refresh();
  const selected = selectIntervention(records);
  if (selected !== null) await rpc.call("loopx_branch", { ...selected });
}
