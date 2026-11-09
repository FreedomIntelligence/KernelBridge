import type { MeasurementLevel, MeasurementProtocol } from "../../generated/task-contract.ts";
import { compareText } from "./canonical.ts";

function normalizeLevel<T extends MeasurementLevel>(level: T): T {
  return { ...level, correctness_case_ids: [...level.correctness_case_ids].sort(compareText) };
}

export function normalizeProtocol(protocol: MeasurementProtocol): MeasurementProtocol {
  return {
    ...protocol,
    levels: {
      smoke: normalizeLevel(protocol.levels.smoke),
      quick: normalizeLevel(protocol.levels.quick),
      full: normalizeLevel(protocol.levels.full),
    },
  };
}
