import type {
  ProtectedPath,
  ProtectedRole,
  SourcePath,
  TrustBoundaryDeclaration,
} from "../../generated/trust-boundary.ts";
import { TrustBoundaryDeclarationError } from "./errors.ts";

interface IndexedProtection {
  readonly index: number;
  readonly value: ProtectedPath;
}

interface ProtectionNode {
  readonly children: Map<string, ProtectionNode>;
  file?: IndexedProtection;
  tree?: IndexedProtection;
}

export interface ProtectionIndex {
  readonly all: ReadonlyMap<string, ProtectionNode>;
  readonly instructions: ReadonlyMap<string, ProtectionNode>;
}

function newNode(): ProtectionNode {
  return { children: new Map() };
}

function insert(roots: Map<string, ProtectionNode>, entry: IndexedProtection): void {
  const { root_id, path, scope } = entry.value;
  const root = roots.get(root_id) ?? newNode();
  roots.set(root_id, root);
  let node: ProtectionNode = root;
  for (const component of path === "." ? [] : path.split("/")) {
    let child: ProtectionNode | undefined = node.children.get(component);
    if (child === undefined) {
      child = newNode();
      node.children.set(component, child);
    }
    node = child;
  }
  // Input order fixes which protected identity is reported when roles overlap.
  node[scope] ??= entry;
}

function earlier(
  left: IndexedProtection | undefined,
  right: IndexedProtection | undefined,
): IndexedProtection | undefined {
  if (left === undefined) return right;
  if (right === undefined) return left;
  return left.index < right.index ? left : right;
}

export function coveringProtection(
  roots: ReadonlyMap<string, ProtectionNode>,
  location: SourcePath,
): ProtectedPath | undefined {
  let node = roots.get(location.root_id);
  let match = node?.tree;
  for (const component of location.path.split("/")) {
    node = node?.children.get(component);
    match = earlier(match, node?.tree);
    if (node === undefined) return match?.value;
  }
  return earlier(match, node?.file)?.value;
}

function checkProtectedPaths(declaration: TrustBoundaryDeclaration): void {
  const seen = new Set<string>();
  const roles = new Set<ProtectedRole>();
  for (const path of declaration.protected_paths) {
    const key = JSON.stringify([path.root_id, path.path, path.scope, path.role]);
    if (seen.has(key)) {
      throw new TrustBoundaryDeclarationError(
        "duplicate_protected_path", `protected ${path.role} path ${path.root_id}/${path.path} is repeated`,
        { boundary_id: declaration.boundary_id, ...path },
      );
    }
    seen.add(key);
    roles.add(path.role);
  }
  const criticalRoles = ["task_contract", "reference", "evaluator", "controller", "git_metadata"] as const;
  for (const role of criticalRoles) {
    if (!roles.has(role)) {
      throw new TrustBoundaryDeclarationError(
        "missing_protected_role", `protected role ${role} is missing`,
        { boundary_id: declaration.boundary_id, role },
      );
    }
  }
}

export function indexProtection(declaration: TrustBoundaryDeclaration): ProtectionIndex {
  checkProtectedPaths(declaration);
  const all = new Map<string, ProtectionNode>();
  const instructions = new Map<string, ProtectionNode>();
  declaration.protected_paths.forEach((value, index) => {
    const entry = { value, index };
    insert(all, entry);
    if (value.role === "instruction_source") insert(instructions, entry);
  });
  return { all, instructions };
}
