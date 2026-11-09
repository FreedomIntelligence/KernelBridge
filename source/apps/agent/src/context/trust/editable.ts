import type { SourcePath, TrustBoundaryDeclaration } from "../../generated/trust-boundary.ts";
import { TrustBoundaryDeclarationError } from "./errors.ts";
import { coveringProtection } from "./protection.ts";
import type { ProtectionIndex } from "./protection.ts";

interface EditableNode {
  readonly children: Map<string, EditableNode>;
  first?: SourcePath;
  file?: SourcePath;
}

function checkDuplicates(declaration: TrustBoundaryDeclaration): void {
  const seen = new Set<string>();
  for (const source of declaration.editable_sources) {
    const key = JSON.stringify([source.root_id, source.path]);
    if (seen.has(key)) {
      throw new TrustBoundaryDeclarationError(
        "duplicate_editable_path", `editable path ${source.root_id}/${source.path} is repeated`,
        { boundary_id: declaration.boundary_id, ...source },
      );
    }
    seen.add(key);
  }
}

function insertEditable(root: EditableNode, source: SourcePath): SourcePath | undefined {
  let node = root;
  for (const component of source.path.split("/")) {
    if (node.file !== undefined) return node.file;
    node.first ??= source;
    let child = node.children.get(component);
    if (child === undefined) {
      child = { children: new Map() };
      node.children.set(component, child);
    }
    node = child;
  }
  if (node.first !== undefined) return node.first;
  node.first = source;
  node.file = source;
  return undefined;
}

function checkAncestry(declaration: TrustBoundaryDeclaration): void {
  const roots = new Map<string, EditableNode>();
  for (const source of declaration.editable_sources) {
    let root = roots.get(source.root_id);
    if (root === undefined) {
      root = { children: new Map() };
      roots.set(source.root_id, root);
    }
    const conflict = insertEditable(root, source);
    if (conflict !== undefined) {
      throw new TrustBoundaryDeclarationError(
        "editable_path_ancestry", `editable files ${source.path} and ${conflict.path} have an ancestor conflict`,
        { boundary_id: declaration.boundary_id, ...source, conflicting_path: conflict.path },
      );
    }
  }
}

export function checkEditable(declaration: TrustBoundaryDeclaration, protection: ProtectionIndex): void {
  checkDuplicates(declaration);
  checkAncestry(declaration);
  for (const source of declaration.editable_sources) {
    const conflict = coveringProtection(protection.all, source);
    if (conflict !== undefined) {
      throw new TrustBoundaryDeclarationError(
        "editable_protected_overlap", `editable ${source.path} overlaps protected ${conflict.role} ${conflict.path}`,
        { boundary_id: declaration.boundary_id, ...source, role: conflict.role, conflicting_path: conflict.path },
      );
    }
  }
}
