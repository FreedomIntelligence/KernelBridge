import type {
  ProtectedRole,
  TrustBoundaryRejectionCode,
} from "../../generated/trust-boundary.ts";

/** Local API diagnostics, not a wire envelope or permission receipt. */
export interface TrustBoundaryRejectionContext {
  readonly boundary_id: string;
  readonly root_id?: string;
  readonly path?: string;
  readonly role?: ProtectedRole;
  readonly source_id?: string;
  readonly capability?: string;
  readonly conflicting_root_id?: string;
  readonly conflicting_path?: string;
  readonly owner?: string;
  readonly record_id?: string;
  readonly digest_scope?: string;
  readonly locator?: string;
}

export class TrustBoundaryDeclarationError extends Error {
  readonly code: TrustBoundaryRejectionCode;
  readonly context: TrustBoundaryRejectionContext;

  constructor(
    code: TrustBoundaryRejectionCode,
    detail: string,
    context: TrustBoundaryRejectionContext,
  ) {
    super(`${code}: boundary ${context.boundary_id}: ${detail}`);
    this.name = "TrustBoundaryDeclarationError";
    this.code = code;
    this.context = Object.freeze({ ...context });
  }
}
