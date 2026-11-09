import type { PairedMeasurement } from "../generated/rsi.ts";

/** Discovery observations may guide the next edit; confirmation samples stay private. */
export function discoveryFeedback(measurement: PairedMeasurement, unitId: string) {
  if (measurement.independent_confirmation) throw new Error("Confirmation data is not discovery feedback");
  const mean = (values: readonly number[]) => values.reduce((sum, value) => sum + value, 0) / values.length;
  return {
    measurement_id: measurement.measurement_id, unit_id: unitId,
    protocol_id: measurement.protocol_id, pairs: measurement.sample_ids.length,
    keep_digest: measurement.keep_digest, remove_digest: measurement.remove_digest,
    keep_correct: measurement.keep_correct, remove_correct: measurement.remove_correct,
    keep_mean_log_score: measurement.keep_correct ? mean(measurement.keep_log_scores) : null,
    remove_mean_log_score: measurement.remove_correct ? mean(measurement.remove_log_scores) : null,
    keep_features: measurement.features, remove_features: measurement.remove_features,
  };
}
