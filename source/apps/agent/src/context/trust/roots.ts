import type { TrustBoundaryDeclaration } from "../../generated/trust-boundary.ts";
import { TrustBoundaryDeclarationError } from "./errors.ts";

export function checkRoots(declaration: TrustBoundaryDeclaration): ReadonlySet<string> {
  const roots = new Set<string>();
  const owners = new Map<string, string>();
  for (const root of declaration.roots) {
    const context = { boundary_id: declaration.boundary_id, root_id: root.root_id };
    if (roots.has(root.root_id)) {
      throw new TrustBoundaryDeclarationError("duplicate_root", `root ${root.root_id} is repeated`, context);
    }
    const { owner, record_id, digest_scope, locator } = root.owner_ref;
    const key = JSON.stringify([owner, record_id, digest_scope, locator]);
    const previousRoot = owners.get(key);
    if (previousRoot !== undefined) {
      throw new TrustBoundaryDeclarationError(
        "duplicate_root_owner", `root ${root.root_id} repeats owner of ${previousRoot}`,
        { ...context, conflicting_root_id: previousRoot, owner, record_id, digest_scope, locator },
      );
    }
    roots.add(root.root_id);
    owners.set(key, root.root_id);
  }
  return roots;
}

export function checkRootReferences(
  declaration: TrustBoundaryDeclaration,
  roots: ReadonlySet<string>,
): void {
  const locations = [
    ...declaration.editable_sources,
    ...declaration.protected_paths,
    ...declaration.instruction_policy.sources.map((source) => ({ ...source.location, source_id: source.source_id })),
  ];
  for (const location of locations) {
    if (!roots.has(location.root_id)) {
      throw new TrustBoundaryDeclarationError(
        "unknown_root", `path ${location.path} references undeclared root ${location.root_id}`,
        { boundary_id: declaration.boundary_id, ...location },
      );
    }
  }
}
