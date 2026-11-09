import type { TrustBoundaryDeclaration } from "../generated/trust-boundary.ts";
import { validateTrustBoundaryDeclaration } from "./trust-boundary.ts";
import { checkEditable } from "./trust/editable.ts";
import { checkCapabilities, checkInstructions } from "./trust/policies.ts";
import { indexProtection } from "./trust/protection.ts";
import { checkRootReferences, checkRoots } from "./trust/roots.ts";

export { TrustBoundaryDeclarationError } from "./trust/errors.ts";
export type { TrustBoundaryRejectionContext } from "./trust/errors.ts";

/** Check a detached supplied declaration; actual enforcement remains unknown. */
export function checkTrustBoundaryDeclaration(value: unknown): TrustBoundaryDeclaration {
  const declaration = validateTrustBoundaryDeclaration(value);
  const roots = checkRoots(declaration);
  checkRootReferences(declaration, roots);
  const protection = indexProtection(declaration);
  checkEditable(declaration, protection);
  checkInstructions(declaration, protection);
  checkCapabilities(declaration);
  return declaration;
}
