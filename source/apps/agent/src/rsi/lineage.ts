import type { EffectRpc } from "../clients/rpc.ts";
import type {
  AcceptanceRequest, RsiBoundaryResult, RsiMeasuredNode, RsiSettledBoundary, RsiSourceSnapshot,
} from "../generated/rsi.ts";
import { artifactDigest } from "./artifacts.ts";
import { pairedInterval, readMeasurement } from "./necessity.ts";
import { invalidatedMeasurements } from "./memory.ts";
import { readRsi, Records, sourceFiles, text } from "./records.ts";

/** Bind a formal measurement to an already acknowledged, immutable Pi boundary. */
export function measuredNode(
  records: Records, acceptanceId: string, boundary: RsiSettledBoundary,
  parent: RsiMeasuredNode | null,
): RsiMeasuredNode {
  const acceptance = records.get(acceptanceId);
  if (!acceptance || acceptance.kind !== "acceptance" || boundary.acceptance_id !== acceptanceId) {
    throw new Error("RSI node requires its original acceptance and native boundary");
  }
  const value = acceptance.value;
  const decision = readRsi<AcceptanceRequest>(value.request, "AcceptanceRequest");
  const measurement = readMeasurement(value.confirmation);
  const retained = artifactDigest(sourceFiles(value.source));
  if (decision.accept && measurement.remove_correct !== true || !measurement.independent_confirmation
      || measurement.measurement_id !== decision.measurement_id || measurement.evidence_ids.length === 0
      || retained !== (decision.accept ? measurement.remove_digest : measurement.keep_digest)
      || boundary.turn_id !== value.turn_id
      || parent !== null && (!parent.accepted || parent.source_digest !== measurement.keep_digest)) {
    throw new Error("RSI node source, parent, turn or confirmation identity differs");
  }
  const initial = records.last("initial");
  if (parent === null && (!initial || artifactDigest(sourceFiles(initial.value.source)) !== measurement.keep_digest)) {
    throw new Error("RSI node has no measured parent or original initial baseline");
  }
  if (parent !== null) {
    const parentAcceptance = records.get(parent.acceptance_id);
    if (!parentAcceptance || parentAcceptance.sequence >= acceptance.sequence || parent.task_id !== value.task_id) {
      throw new Error("RSI node parent must precede its acceptance in the same task");
    }
  }
  const scores = measurement.remove_log_scores;
  const snapshot = value.source_snapshot === undefined ? undefined
    : readRsi<RsiSourceSnapshot>(value.source_snapshot, "RsiSourceSnapshot");
  if (snapshot && snapshot.remove.source_digest !== measurement.remove_digest) throw new Error("RSI node commit differs from confirmed source");
  const interval = pairedInterval(scores);
  const mean = scores.length === 0 ? null : scores.reduce((sum, score) => sum + score / scores.length, 0);
  return readRsi<RsiMeasuredNode>({
    schema_version: "kernelcat.rsi-node/v1", node_id: acceptanceId + "/node",
    task_id: text(value.task_id, "node task"), parent_id: parent?.node_id ?? null,
    acceptance_id: acceptanceId, experiment_id: decision.experiment_id,
    source_digest: measurement.remove_digest, retained_source_digest: retained,
    baseline_digest: measurement.keep_digest, accepted: decision.accept,
    measurement_id: measurement.measurement_id, environment_id: measurement.environment_id,
    protocol_id: measurement.protocol_id, correct: measurement.remove_correct,
    score: { mean, lower: interval?.lower ?? null, upper: interval?.upper ?? null, samples: scores.length },
    boundary,
    ...(snapshot ? { source_commit: snapshot.remove } : {}),
  }, "RsiMeasuredNode");
}

/** O(R + N) local work; pending boundaries are read once per invocation, never rerun. */
export async function captureMeasuredNodes(records: Records, rpc: EffectRpc): Promise<number> {
  const nodes = records.all("node").map((record) => readRsi<RsiMeasuredNode>(record.value, "RsiMeasuredNode"));
  const captured = new Map(nodes.map((node) => [node.acceptance_id, node]));
  const bySource = new Map<string, RsiMeasuredNode>();
  const invalid = invalidatedMeasurements(records);
  let count = 0;
  for (const acceptance of records.all("acceptance")) {
    if (acceptance.value.turn_id === undefined) continue;
    const decision = readRsi<AcceptanceRequest>(acceptance.value.request, "AcceptanceRequest");
    const previous = captured.get(acceptance.record_id);
    if (previous) {
      if (previous.accepted) bySource.set(previous.source_digest, previous);
      continue;
    }
    if (invalid.has(decision.measurement_id)) continue;
    const result = readRsi<RsiBoundaryResult>(await rpc.call("rsi_boundary", {
      acceptance_id: acceptance.record_id,
    }), "RsiBoundaryResult");
    if (result.boundary === null) continue;
    const measurement = readMeasurement(acceptance.value.confirmation);
    const parent = bySource.get(measurement.keep_digest) ?? null;
    const node = measuredNode(records, acceptance.record_id, result.boundary, parent);
    await records.write(node.node_id, "node", node);
    if (node.accepted) bySource.set(node.source_digest, node);
    count += 1;
  }
  return count;
}
