import type { TrustBoundaryDeclaration } from "../../generated/trust-boundary.ts";
import { TrustBoundaryDeclarationError } from "./errors.ts";
import { coveringProtection } from "./protection.ts";
import type { ProtectionIndex } from "./protection.ts";

function checkInstructionIds(declaration: TrustBoundaryDeclaration): void {
  const seen = new Set<string>();
  for (const source of declaration.instruction_policy.sources) {
    if (seen.has(source.source_id)) {
      throw new TrustBoundaryDeclarationError(
        "duplicate_instruction_source", `instruction source ${source.source_id} is repeated`,
        { boundary_id: declaration.boundary_id, source_id: source.source_id, ...source.location },
      );
    }
    seen.add(source.source_id);
  }
}

export function checkInstructions(declaration: TrustBoundaryDeclaration, protection: ProtectionIndex): void {
  checkInstructionIds(declaration);
  for (const source of declaration.instruction_policy.sources) {
    if (coveringProtection(protection.instructions, source.location) === undefined) {
      throw new TrustBoundaryDeclarationError(
        "instruction_unprotected", `instruction source ${source.source_id} at ${source.location.path} is unprotected`,
        { boundary_id: declaration.boundary_id, source_id: source.source_id, role: "instruction_source", ...source.location },
      );
    }
  }
  for (const source of declaration.instruction_policy.sources) {
    if (source.loading === "automatic" && declaration.instruction_policy.automatic_loading === "disabled") {
      throw new TrustBoundaryDeclarationError(
        "automatic_loading_disabled", `automatic instruction source ${source.source_id} is disabled`,
        { boundary_id: declaration.boundary_id, source_id: source.source_id, ...source.location },
      );
    }
  }
}

export function checkCapabilities(declaration: TrustBoundaryDeclaration): void {
  const seen = new Set<string>();
  for (const capability of declaration.network_policy.capabilities) {
    if (seen.has(capability)) {
      throw new TrustBoundaryDeclarationError(
        "duplicate_capability", `network capability ${capability} is repeated`,
        { boundary_id: declaration.boundary_id, capability },
      );
    }
    seen.add(capability);
  }
}
