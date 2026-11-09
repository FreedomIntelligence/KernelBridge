// Ordinal comparison is independent of host locale and supplies every sort tie-break.
export function compareText(left: string, right: string): number {
  return left < right ? -1 : left > right ? 1 : 0;
}

export function canonicalValue<T>(value: T): T {
  if (Array.isArray(value)) {
    return value.map((item) => canonicalValue(item)) as T;
  }
  if (value !== null && typeof value === "object") {
    return Object.fromEntries(Object.entries(value)
      .sort(([left], [right]) => compareText(left, right))
      .map(([key, item]) => [key, canonicalValue(item)])) as T;
  }
  return value;
}

// Unlike JSON.stringify, retain -0 when comparing duplicate observations.
export function canonicalKey(value: unknown): string {
  if (typeof value === "number" && Object.is(value, -0)) {
    return "-0";
  }
  if (Array.isArray(value)) {
    return `[${value.map(canonicalKey).join(",")}]`;
  }
  if (value !== null && typeof value === "object") {
    const entries = Object.entries(value).sort(([left], [right]) => compareText(left, right));
    return `{${entries.map(([key, item]) => `${JSON.stringify(key)}:${canonicalKey(item)}`).join(",")}}`;
  }
  return JSON.stringify(value);
}
