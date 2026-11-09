// Engineering check selection only. The existing CI owners execute their own gates.
import type {
  ExecutableIdentity, ExecutableVersions, IdentityObservation, ModelIdentity,
  NativeVersions, PackageIdentity, RuleVersions, SourceIdentity, VersionLock, VersionProbe,
} from "../generated/version-lock.ts";
import { validateVersionLock } from "../harness/version-lock.ts";

export type OfflineCheckId = "host" | "pi-unit" | "pi-integration" | "loopx" | "backend" | "wiki";

export interface OfflineCheck {
  readonly id: OfflineCheckId;
  /** Repository-relative working directory; argv is an argument vector, never shell text. */
  readonly cwd: string;
  readonly argv: readonly string[];
  readonly environment: Readonly<Record<string, string>>;
  /** Existing prerequisites, not installation instructions or permission to install. */
  readonly requirements: readonly string[];
  readonly sources: readonly string[];
}

export type VersionComponent = string | IdentityObservation | SourceIdentity | VersionProbe;

export interface VersionChange {
  /** JSON Pointer, including escaped package/route member names. */
  readonly path: string;
  readonly kind: "initial" | "added" | "removed" | "changed";
  readonly previous?: VersionComponent;
  readonly current?: VersionComponent;
  readonly checks: readonly OfflineCheckId[];
  readonly reason: string;
}

export interface VersionTestPlan {
  readonly observation: "initial" | "comparison";
  readonly changes: readonly VersionChange[];
  readonly unknowns: readonly { readonly path: string; readonly reason: string }[];
  readonly checks: readonly OfflineCheck[];
  readonly limitations: readonly string[];
}

const PI_CWD = "apps/agent";
const CI_SOURCE = "scripts/ci.py";
const WORKSPACE_SOURCE = ".github/workflows/workspace-contracts.yml";
const PI_SOURCE = ".github/workflows/pi-worker.yml";
const LOOPX_SOURCE = ".github/workflows/loopx-integration.yml";
const PYTHON_ENVIRONMENT = { PYTHONHASHSEED: "0", PYTHONDONTWRITEBYTECODE: "1" };
const HOST_DEPENDENCIES = [
  "Python 3.11 or 3.12 with kernelcat-executor[dev]; scripts/constraints-ci.txt",
  "Node 22.19.0 and existing packages from apps/agent/package-lock.json",
];
const CHECKS: readonly OfflineCheck[] = [
  {
    id: "host", cwd: ".", argv: ["python", CI_SOURCE, "--suite", "host"],
    environment: PYTHON_ENVIRONMENT, requirements: HOST_DEPENDENCIES,
    sources: [CI_SOURCE, WORKSPACE_SOURCE],
  },
  {
    id: "pi-unit", cwd: ".", argv: ["python", CI_SOURCE, "--suite", "pi-unit"],
    environment: { PYTHONHASHSEED: "0", KERNELCAT_REQUIRE_PI: "1" },
    requirements: HOST_DEPENDENCIES,
    sources: [CI_SOURCE, PI_SOURCE, `${PI_CWD}/package.json`],
  },
  {
    id: "pi-integration", cwd: ".", argv: ["python", CI_SOURCE, "--suite", "pi-integration"],
    environment: { PYTHONHASHSEED: "0", KERNELCAT_REQUIRE_PI: "1" },
    requirements: HOST_DEPENDENCIES,
    sources: [CI_SOURCE, PI_SOURCE, `${PI_CWD}/package.json`],
  },
  {
    id: "loopx", cwd: ".",
    argv: ["python", CI_SOURCE, "--suite", "loopx", "--tests-only"],
    environment: { PYTHONHASHSEED: "0", KERNELCAT_REQUIRE_LOOPX: "1" },
    requirements: [
      "Python 3.11 with kernelcat-executor[dev]; scripts/constraints-ci.txt",
      "Node 22.22.3 and existing packages from apps/agent/package-lock.json",
      "Optional hash-pinned LoopX from apps/agent/loopx-requirements.txt must already be installed",
    ],
    sources: [LOOPX_SOURCE, "apps/agent/loopx-requirements.txt", CI_SOURCE],
  },
  {
    id: "backend", cwd: ".", argv: ["python", CI_SOURCE, "--suite", "backend"],
    environment: PYTHON_ENVIRONMENT,
    requirements: [
      "Python 3.11 with kernelcat-executor[dev,backend]; scripts/constraints-ci.txt",
      "Optional CPU-only torch==2.7.1+cpu as specified in workspace-contracts.yml",
    ],
    sources: [CI_SOURCE, WORKSPACE_SOURCE],
  },
  {
    id: "wiki", cwd: ".", argv: ["python", CI_SOURCE, "--suite", "wiki"],
    environment: PYTHON_ENVIRONMENT,
    requirements: ["Python 3.11 with wiki/engine and wiki/engine/requirements-dev.txt; scripts/constraints-ci.txt"],
    sources: [CI_SOURCE, WORKSPACE_SOURCE],
  },
];

interface MarkerRule {
  readonly checks: readonly OfflineCheckId[];
  readonly reason: string;
}

interface Marker extends MarkerRule {
  readonly value: VersionComponent;
}

const HOST_PI: readonly OfflineCheckId[] = ["host", "pi-unit", "pi-integration"];
const ALL_CHECKS: readonly OfflineCheckId[] = CHECKS.map((check) => check.id);
const CONTROL: MarkerRule = { checks: HOST_PI, reason: "Host/Pi control and protocol contracts may change." };
const OPAQUE: MarkerRule = {
  checks: ALL_CHECKS,
  reason: "Opaque identity does not locate changed behavior; conservatively select every listed boundary, including optional checks.",
};
const RESOURCE: MarkerRule = {
  checks: ALL_CHECKS,
  reason: "Resource ownership spans host tools, Wiki and backend contracts; the digest does not identify the affected resource.",
};
const NATIVE: MarkerRule = {
  checks: ["host", "backend"], reason: "Select host/native boundary contracts; CPU fixtures do not qualify the native environment.",
};
const LOOPX: MarkerRule = {
  checks: [...HOST_PI, "loopx"], reason: "Select host/Pi contracts and the explicit optional LoopX offline lane.",
};

// Whole observations/probes/source envelopes include all evidence, unknown reasons and
// optional members. No equal version label can hide changed provenance. Exhaustive
// field maps make authority additions a type error instead of an unselected change.
type DirectField = Exclude<keyof VersionLock, "packages" | "executables" | "models" | "rules" | "native_environment">;
const DIRECT_MARKERS = {
  schema_version: OPAQUE,
  runtime_inputs_digest: OPAQUE,
  configuration_digest: OPAQUE,
  resource_manifest_digest: RESOURCE,
  source: OPAQUE,
  host_runtime: OPAQUE,
  kb_snapshot: { checks: [...HOST_PI, "wiki"], reason: "Select host/Pi resource loading and the Wiki contract owner." },
  tool_schema: CONTROL,
} satisfies Record<DirectField, MarkerRule>;
const PACKAGE_MARKERS = {
  declared_constraint: OPAQUE,
  installed: OPAQUE,
} satisfies Record<keyof PackageIdentity, MarkerRule>;
const EXECUTABLE_MARKERS = {
  node: LOOPX, pi: CONTROL, loopx: LOOPX,
} satisfies Record<keyof ExecutableVersions, MarkerRule>;
const EXECUTABLE_FIELDS = { identity: true, probe: true } satisfies Record<keyof ExecutableIdentity, true>;
const MODEL_MARKERS = {
  requested_provider: CONTROL,
  requested_model: CONTROL,
  parameters_digest: CONTROL,
  reported_model: CONTROL,
  resolved_revision: CONTROL,
} satisfies Record<keyof ModelIdentity, MarkerRule>;
const RULE_MARKERS = {
  controller: CONTROL, context_compiler: CONTROL, prune_policy: CONTROL, replay_rules: CONTROL,
} satisfies Record<keyof RuleVersions, MarkerRule>;
const NATIVE_MARKERS = {
  compiler: NATIVE, driver: NATIVE, evaluation_image: NATIVE,
} satisfies Record<keyof NativeVersions, MarkerRule>;

function ordinal(left: string, right: string): number {
  return left < right ? -1 : left > right ? 1 : 0;
}

function pointer(...members: string[]): string {
  return `/${members.map((member) => member.replaceAll("~", "~0").replaceAll("/", "~1")).join("/")}`;
}

function fields<T extends object>(
  markers: Map<string, Marker>, prefix: readonly string[], value: T,
  rules: Record<keyof T, MarkerRule>,
): void {
  for (const field of Object.keys(rules) as (keyof T & string)[]) {
    markers.set(pointer(...prefix, field), { ...rules[field], value: value[field] as VersionComponent });
  }
}

function markersFor(lock: VersionLock): Map<string, Marker> {
  const markers = new Map<string, Marker>();
  for (const field of Object.keys(DIRECT_MARKERS) as DirectField[]) {
    markers.set(pointer(field), { ...DIRECT_MARKERS[field], value: lock[field] });
  }
  for (const [name, value] of Object.entries(lock.packages)) {
    fields(markers, ["packages", name], value, PACKAGE_MARKERS);
  }
  for (const [name, value] of Object.entries(lock.models)) {
    fields(markers, ["models", name], value, MODEL_MARKERS);
  }
  for (const name of Object.keys(EXECUTABLE_MARKERS) as (keyof ExecutableVersions)[]) {
    for (const field of Object.keys(EXECUTABLE_FIELDS) as (keyof ExecutableIdentity)[]) {
      if (Object.hasOwn(lock.executables[name], field)) {
        markers.set(pointer("executables", name, field), {
          ...EXECUTABLE_MARKERS[name], value: lock.executables[name][field]!,
        });
      }
    }
  }
  fields(markers, ["rules"], lock.rules, RULE_MARKERS);
  fields(markers, ["native_environment"], lock.native_environment, NATIVE_MARKERS);
  return markers;
}

function canonical(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
  if (value !== null && typeof value === "object") {
    const entries = Object.entries(value).sort(([left], [right]) => ordinal(left, right));
    return `{${entries.map(([key, item]) => `${JSON.stringify(key)}:${canonical(item)}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

function validated(value: unknown, role: "current" | "previous"): VersionLock {
  try {
    return validateVersionLock(value);
  } catch (error) {
    throw new Error(`Cannot plan checks: invalid ${role} VersionLock: ${String(error)}`, { cause: error });
  }
}

function orderedComponent(value: VersionComponent): VersionComponent {
  // Retained envelopes must serialize identically after member-order permutations.
  // JSON.parse preserves own __proto__ members without invoking object setters.
  return JSON.parse(canonical(value)) as VersionComponent;
}

function changesBetween(current: Map<string, Marker>, previous?: Map<string, Marker>): VersionChange[] {
  const paths = [...new Set([...current.keys(), ...(previous?.keys() ?? [])])].sort(ordinal);
  const changes: VersionChange[] = [];
  for (const path of paths) {
    const before = previous?.get(path);
    const after = current.get(path);
    if (before && after && canonical(before.value) === canonical(after.value)) continue;
    const marker = after ?? before!;
    changes.push({
      path, kind: previous === undefined ? "initial" : !before ? "added" : !after ? "removed" : "changed",
      ...(before ? { previous: orderedComponent(before.value) } : {}),
      ...(after ? { current: orderedComponent(after.value) } : {}),
      checks: marker.checks, reason: marker.reason,
    });
  }
  return changes;
}

/**
 * Validates each supplied document once, then plans without I/O or effects.
 * For n JSON members and m markers: O(n log n + m log m) time, O(n + m) space.
 * Open package membership and opaque/unlocalized changes select all known checks;
 * unavailable optional dependencies remain prerequisites, never silent skips.
 */
export function planVersionTests(currentInput: unknown, previousInput?: unknown): VersionTestPlan {
  const current = markersFor(validated(currentInput, "current"));
  const previous = previousInput === undefined ? undefined : markersFor(validated(previousInput, "previous"));
  const changes = changesBetween(current, previous);
  const selected = new Set(changes.flatMap((change) => change.checks));
  const unknowns = [...current.entries()].sort(([left], [right]) => ordinal(left, right))
    .flatMap(([path, { value }]) => typeof value === "object" && "state" in value && value.state === "unknown"
      ? [{ path, reason: value.reason }] : []);
  // Also detach table-owned arrays/records so callers cannot change later plans.
  return structuredClone({
    observation: previous === undefined ? "initial" : "comparison",
    changes, unknowns, checks: CHECKS.filter((check) => selected.has(check.id)),
    limitations: [
      "This plan runs no checks, certifies no completed checks and does not qualify runtime reuse.",
      "Unknown observations remain unresolved even when unchanged; no empty diff proves qualification.",
      "Offline model/native contract fixtures do not establish model quality, served weight identity, device correctness or speed.",
      "Commands use repository-relative cwd; prerequisites must already exist. Dispatchers set their component import paths and create external reports.",
    ],
  });
}
