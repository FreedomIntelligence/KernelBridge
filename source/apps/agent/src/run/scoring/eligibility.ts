import type { ScoringObservation, TaskContract } from "../../generated/task-contract.ts";
import { canonicalKey, compareText } from "./canonical.ts";
import { normalizeProtocol } from "./protocol.ts";
import type { ExclusionCode, ExclusionReason } from "./types.ts";

function reason(code: ExclusionCode, path: string, detail: string): ExclusionReason {
  return { code, path, detail };
}

export function orderReasons(reasons: ReadonlyArray<ExclusionReason>): ReadonlyArray<ExclusionReason> {
  return [...reasons].sort((left, right) => compareText(left.code, right.code)
    || compareText(left.path, right.path) || compareText(left.detail, right.detail));
}

export function bindingReasons(
  contract: TaskContract, digest: string, observation: ScoringObservation,
): ExclusionReason[] {
  const reasons: ExclusionReason[] = [];
  const bindings = [
    ["task_contract_digest", digest, observation.task_contract_digest, "contract_digest_mismatch"],
    ["casepack_digest", contract.input_domain.casepack_digest, observation.casepack_digest, "casepack_digest_mismatch"],
    ["baseline", canonicalKey(contract.baseline), canonicalKey(observation.baseline), "baseline_mismatch"],
    ["timing", canonicalKey(contract.timing), canonicalKey(observation.timing), "timing_mismatch"],
    ["measurement_protocol", canonicalKey(normalizeProtocol(contract.measurement_protocol)),
      canonicalKey(normalizeProtocol(observation.measurement_protocol)), "measurement_protocol_mismatch"],
  ] as const;
  for (const [path, expected, supplied, code] of bindings) {
    if (expected !== supplied) {
      reasons.push(reason(code, path, "supplied binding differs from TaskContract"));
    }
  }
  if (observation.stage === "screen") {
    reasons.push(reason("screening_stage", "stage", "only confirm/final observations can be scored"));
  }
  reasons.push(...deviceReasons(contract, observation));
  return reasons;
}

function deviceReasons(contract: TaskContract, observation: ScoringObservation): ExclusionReason[] {
  const reasons: ExclusionReason[] = [];
  if (typeof observation.environment !== "string") {
    reasons.push(reason("unknown_environment", "environment", observation.environment.reason));
  }
  if (observation.device.status === "unknown") {
    reasons.push(reason("unknown_device", "device", observation.device.reason));
    return reasons;
  }
  if (observation.device.vendor !== contract.target.vendor) {
    reasons.push(reason("target_vendor_mismatch", "device.vendor", "device vendor contradicts target"));
  }
  if (observation.device.architecture !== contract.target.architecture) {
    reasons.push(reason("target_architecture_mismatch", "device.architecture", "device architecture contradicts target"));
  }
  return reasons;
}

export function domainReasons(
  contract: TaskContract, caseIds: ReadonlyArray<string>, observation: ScoringObservation,
): ExclusionReason[] {
  const declaredCases = new Set(caseIds);
  const reasons: ExclusionReason[] = [];
  for (const caseId of caseIds) {
    if (!Object.hasOwn(observation.correctness, caseId)) {
      reasons.push(reason("missing_correctness", `correctness/${caseId}`, "task case has no correctness verdict"));
    } else if (observation.correctness[caseId] !== "passed") {
      const code = observation.correctness[caseId] === "failed" ? "failed_correctness" : "unknown_correctness";
      reasons.push(reason(code, `correctness/${caseId}`, "task case correctness is not passed"));
    }
  }
  for (const caseId of Object.keys(observation.correctness)) {
    if (!declaredCases.has(caseId)) {
      reasons.push(reason("extra_correctness", `correctness/${caseId}`, "case ID is not declared in TaskContract"));
    }
  }
  reasons.push(...measurementReasons(contract, caseIds, observation));
  const allowed = new Set(contract.specialization_policy.allowed.map((specialization) => specialization.id));
  for (const id of observation.specialization_ids) {
    if (!allowed.has(id)) {
      reasons.push(reason("undeclared_specialization", `specialization_ids/${id}`, "specialization ID is not declared"));
    }
  }
  return reasons;
}

function measurementReasons(
  contract: TaskContract, caseIds: ReadonlyArray<string>, observation: ScoringObservation,
): ExclusionReason[] {
  const reasons: ExclusionReason[] = [];
  if (contract.scoring.kind !== "official") {
    for (const caseId of caseIds) {
      if (!Object.hasOwn(observation.measurements, caseId)) {
        reasons.push(reason("missing_measurement", `measurements/${caseId}`, "weighted score requires a measured pair"));
      } else if (observation.measurements[caseId].status === "unknown") {
        reasons.push(reason("unknown_measurement", `measurements/${caseId}`, "weighted score requires a measured pair"));
      }
    }
  }
  const declaredCases = new Set(caseIds);
  for (const caseId of Object.keys(observation.measurements)) {
    if (!declaredCases.has(caseId)) {
      reasons.push(reason("extra_measurement", `measurements/${caseId}`, "case ID is not declared in TaskContract"));
    }
  }
  return reasons;
}

export function officialReasons(contract: TaskContract, observation: ScoringObservation): ExclusionReason[] {
  if (contract.scoring.kind !== "official") {
    return [];
  }
  if (observation.official_score === undefined) {
    return [reason("missing_official_score", "official_score", "official score must be supplied by its authority")];
  }
  if (typeof observation.official_score !== "number") {
    return [reason("unknown_official_score", "official_score", observation.official_score.reason)];
  }
  return [];
}
