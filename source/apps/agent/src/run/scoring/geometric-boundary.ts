import type { BinaryValue } from "./numeric.ts";

interface Interval {
  readonly lower: bigint;
  readonly upper: bigint;
}

interface WeightedRatio {
  readonly weight: bigint;
  readonly numerator: bigint;
  readonly denominator: bigint;
  readonly exponent: number;
}

function floorDivide(numerator: bigint, denominator: bigint): bigint {
  const quotient = numerator / denominator;
  return numerator < 0n && numerator % denominator !== 0n ? quotient - 1n : quotient;
}

function ceilDivide(numerator: bigint, denominator: bigint): bigint {
  return -floorDivide(-numerator, denominator);
}

// On [1, 2], log(x) = 2 atanh((x-1)/(x+1)). Directed integer
// rounding encloses every series term; the omitted positive tail is bounded.
function mantissaLog(numerator: bigint, denominator: bigint, precision: number): Interval {
  if (numerator === denominator) {
    return { lower: 0n, upper: 0n };
  }
  const scale = 1n << BigInt(precision);
  const dividend = (numerator - denominator) * scale;
  const divisor = numerator + denominator;
  let powerLower = dividend / divisor;
  let powerUpper = ceilDivide(dividend, divisor);
  const squareLower = powerLower * powerLower / scale;
  const squareUpper = ceilDivide(powerUpper * powerUpper, scale);
  let lower = 0n;
  let upper = 0n;
  for (let index = 0; index < precision; index += 1) {
    const odd = BigInt(2 * index + 1);
    lower += powerLower / odd;
    upper += ceilDivide(powerUpper, odd);
    powerLower = powerLower * squareLower / scale;
    powerUpper = ceilDivide(powerUpper * squareUpper, scale);
  }
  const tail = ceilDivide(3n * powerUpper, BigInt(2 * precision + 1));
  return { lower: 2n * lower, upper: 2n * upper + tail };
}

function relativeLog(ratio: WeightedRatio, precision: number, logTwo: Interval): Interval {
  let numerator = ratio.numerator;
  let denominator = ratio.denominator;
  let exponent = ratio.exponent;
  // Reduce to [1, 2] before the series, including subnormal durations.
  while (numerator >= 2n * denominator) {
    denominator *= 2n;
    exponent += 1;
  }
  while (numerator < denominator) {
    numerator *= 2n;
    exponent -= 1;
  }
  const mantissa = mantissaLog(numerator, denominator, precision);
  const factor = BigInt(exponent);
  return {
    lower: mantissa.lower + factor * (exponent >= 0 ? logTwo.lower : logTwo.upper),
    upper: mantissa.upper + factor * (exponent >= 0 ? logTwo.upper : logTwo.lower),
  };
}

function meanRelativeLog(ratios: ReadonlyArray<WeightedRatio>, precision: number): Interval {
  const logTwo = mantissaLog(2n, 1n, precision);
  let lower = 0n;
  let upper = 0n;
  let weightSum = 0n;
  for (const ratio of ratios) {
    const interval = relativeLog(ratio, precision, logTwo);
    lower += interval.lower * ratio.weight;
    upper += interval.upper * ratio.weight;
    weightSum += ratio.weight;
  }
  return { lower: floorDivide(lower, weightSum), upper: ceilDivide(upper, weightSum) };
}

function greatestCommonDivisor(left: bigint, right: bigint): bigint {
  while (right !== 0n) {
    [left, right] = [right, left % right];
  }
  return left;
}

function addFactor(factors: Map<bigint, bigint>, base: bigint, exponent: bigint): void {
  if (base === 1n || exponent === 0n) {
    return;
  }
  const combined = (factors.get(base) ?? 0n) + exponent;
  if (combined === 0n) {
    factors.delete(base);
  } else {
    factors.set(base, combined);
  }
}

function splitCommonFactor(factors: Map<bigint, bigint>): boolean {
  const entries = [...factors];
  for (let left = 0; left < entries.length; left += 1) {
    for (let right = left + 1; right < entries.length; right += 1) {
      const [a, powerA] = entries[left];
      const [b, powerB] = entries[right];
      const common = greatestCommonDivisor(a, b);
      if (common > 1n) {
        factors.delete(a);
        factors.delete(b);
        addFactor(factors, common, powerA + powerB);
        addFactor(factors, a / common, powerA);
        addFactor(factors, b / common, powerB);
        return true;
      }
    }
  }
  return false;
}

// Binary weights share a power-of-two denominator. Equality with a reference is
// therefore a rational product identity. GCD splitting proves equality without
// materializing products raised to potentially enormous integer weights.
function productIsOne(ratios: ReadonlyArray<WeightedRatio>): boolean {
  const factors = new Map<bigint, bigint>();
  for (const ratio of ratios) {
    addFactor(factors, ratio.numerator, ratio.weight);
    addFactor(factors, ratio.denominator, -ratio.weight);
    addFactor(factors, 2n, BigInt(ratio.exponent) * ratio.weight);
  }
  while (splitCommonFactor(factors)) {
    // Each split reduces the product of distinct bases; no factorization is needed.
  }
  return factors.size === 0;
}

function fixedToNumber(value: bigint, precision: number): number {
  const magnitude = value < 0n ? -value : value;
  const shift = Math.max(0, magnitude.toString(2).length - 53);
  const leading = Number(magnitude >> BigInt(shift));
  return (value < 0n ? -leading : leading) * 2 ** (shift - precision);
}

interface ResolvedLog {
  readonly interval: Interval;
  readonly precision: number;
}

function weightedRatios(
  weights: ReadonlyArray<BinaryValue>, baselines: ReadonlyArray<BinaryValue>,
  candidates: ReadonlyArray<BinaryValue>,
): ReadonlyArray<WeightedRatio> {
  const weightExponent = weights.reduce((smallest, weight) => Math.min(smallest, weight.exponent), 0);
  return weights.map((weight, index) => ({
    weight: weight.coefficient << BigInt(weight.exponent - weightExponent),
    numerator: baselines[index].coefficient,
    denominator: candidates[index].coefficient,
    exponent: baselines[index].exponent - candidates[index].exponent,
  }));
}

function relativeTo(ratios: ReadonlyArray<WeightedRatio>, reference: BinaryValue): ReadonlyArray<WeightedRatio> {
  return ratios.map((ratio) => ({
    ...ratio,
    denominator: ratio.denominator * reference.coefficient,
    exponent: ratio.exponent - reference.exponent,
  }));
}

function resolveRelativeLog(ratios: ReadonlyArray<WeightedRatio>): ResolvedLog {
  let checkedEquality = false;
  for (let precision = 128; ; precision *= 2) {
    const interval = meanRelativeLog(ratios, precision);
    if (interval.lower > 0n || interval.upper < 0n) {
      return { interval, precision };
    }
    if (!checkedEquality && productIsOne(ratios)) {
      return { interval: { lower: 0n, upper: 0n }, precision };
    }
    checkedEquality = true;
    // A non-unit rational product has a nonzero logarithm; shrinking interval
    // bounds eventually decide its sign. Equality was resolved exactly above.
  }
}

export function upperBoundaryGeometricMean(
  weights: ReadonlyArray<BinaryValue>, baselines: ReadonlyArray<BinaryValue>,
  candidates: ReadonlyArray<BinaryValue>,
): number {
  const ratios = weightedRatios(weights, baselines, candidates);
  const reference = { coefficient: (1n << 53n) - 1n, exponent: 971 };
  const { interval, precision } = resolveRelativeLog(relativeTo(ratios, reference));
  if (interval.lower > 0n) {
    return Infinity;
  }
  if (interval.upper < 0n) {
    const delta = fixedToNumber((interval.lower + interval.upper) / 2n, precision);
    return Number.MAX_VALUE * Math.exp(delta);
  }
  return Number.MAX_VALUE;
}

function compareToMidpoint(ratios: ReadonlyArray<WeightedRatio>, units: bigint): number {
  const reference = { coefficient: 2n * units + 1n, exponent: -1075 };
  const { interval } = resolveRelativeLog(relativeTo(ratios, reference));
  return interval.lower > 0n ? 1 : interval.upper < 0n ? -1 : 0;
}

export function lowerBoundaryGeometricMean(
  weights: ReadonlyArray<BinaryValue>, baselines: ReadonlyArray<BinaryValue>,
  candidates: ReadonlyArray<BinaryValue>,
): number {
  const ratios = weightedRatios(weights, baselines, candidates);
  let lower = 0n;
  let upper = 1n << 53n;
  // Subnormals and the first normal binade share MIN_VALUE spacing. Compare
  // exact midpoints, including zero's midpoint; do not round via log/exp.
  while (lower < upper) {
    const units = (lower + upper) / 2n;
    const comparison = compareToMidpoint(ratios, units);
    if (comparison > 0 || (comparison === 0 && units % 2n === 1n)) {
      lower = units + 1n;
    } else {
      upper = units;
    }
  }
  return Number(lower) * Number.MIN_VALUE;
}
