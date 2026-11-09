import type { TaskContract } from "../../generated/task-contract.ts";
import { compareText } from "./canonical.ts";

function invalidReference(path: string, detail: string): never {
  throw new Error(`Invalid scoring TaskContract at ${path}: ${detail}`);
}

export function validateScoringReferences(contract: TaskContract): ReadonlyArray<string> {
  const caseIds = Object.keys(contract.input_domain.cases).sort(compareText);
  const declaredCases = new Set(caseIds);
  const specializationIds = new Set<string>();
  for (const specialization of contract.specialization_policy.allowed) {
    const path = `specialization_policy.allowed/${specialization.id}`;
    if (specializationIds.has(specialization.id)) {
      invalidReference(path, "duplicate specialization ID");
    }
    specializationIds.add(specialization.id);
    for (const caseId of specialization.case_ids) {
      if (!declaredCases.has(caseId)) {
        invalidReference(`${path}/case_ids/${caseId}`, "case ID is not declared in input_domain.cases");
      }
    }
  }
  for (const [level, policy] of Object.entries(contract.measurement_protocol.levels)) {
    for (const caseId of policy.correctness_case_ids) {
      if (!declaredCases.has(caseId)) {
        invalidReference(`measurement_protocol.levels/${level}/correctness_case_ids/${caseId}`,
          "case ID is not declared in input_domain.cases");
      }
    }
  }
  validateStrides(contract, caseIds);
  return caseIds;
}

function validateStrides(contract: TaskContract, caseIds: ReadonlyArray<string>): void {
  for (const caseId of caseIds) {
    const domain = contract.input_domain.cases[caseId];
    for (const side of ["inputs", "outputs"] as const) {
      for (const name of Object.keys(domain[side]).sort(compareText)) {
        const tensor = domain[side][name];
        if (tensor.strides !== undefined && tensor.strides.length !== tensor.shape.length) {
          invalidReference(`input_domain.cases/${caseId}/${side}/${name}/strides`,
            "stride count must equal tensor rank");
        }
      }
    }
  }
}
