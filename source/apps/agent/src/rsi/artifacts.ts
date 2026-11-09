import { createHash } from "node:crypto";
import type { ChangeUnit, Decomposition, SourceFile } from "../generated/rsi.ts";
import { canonicalKey, compareText } from "../run/scoring/canonical.ts";
import { readRsi } from "./records.ts";

export function artifactDigest(value: unknown): string {
  return createHash("sha256").update(canonicalKey(value)).digest("hex");
}

export function removeUnits(
  files: readonly SourceFile[], units: readonly ChangeUnit[], removed: readonly string[],
): readonly SourceFile[] {
  const byId = new Map(units.map((unit) => [unit.unit_id, unit]));
  if (byId.size !== units.length || new Set(removed).size !== removed.length) {
    throw new Error("RSI rollback repeats a unit identity");
  }
  const texts = new Map(files.map((file) => [file.path, file.text]));
  for (const identity of [...removed].sort(compareText)) {
    const unit = byId.get(identity);
    if (!unit) throw new Error(`Unknown RSI rollback unit: ${identity}`);
    const content = texts.get(unit.path);
    if (content === undefined || unit.after.length === 0 || content.split(unit.after).length !== 2) {
      throw new Error(`RSI unit has no unique reversible anchor: ${identity}`);
    }
    texts.set(unit.path, content.replace(unit.after, () => unit.before));
  }
  // Preserve the original validated source order, including Unicode paths.
  return files.map((file) => ({ path: file.path, text: texts.get(file.path)! }));
}

export function readDecomposition(
  value: unknown, baseline: readonly SourceFile[], candidate: readonly SourceFile[], maximum: number,
): Decomposition {
  const split = readRsi<Decomposition>(value, "Decomposition");
  if (split.units.length === 0 || split.units.length > maximum) {
    throw new Error("RSI decomposition has no units or exceeds its admitted limit");
  }
  const restored = removeUnits(candidate, split.units, split.units.map((unit) => unit.unit_id));
  if (artifactDigest(restored) !== artifactDigest(baseline)) {
    throw new Error("RSI decomposition does not restore the exact accepted baseline");
  }
  for (const unit of split.units) removeUnits(candidate, split.units, [unit.unit_id]);
  return split;
}
