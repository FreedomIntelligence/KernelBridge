import { lowerBoundaryGeometricMean, upperBoundaryGeometricMean } from "./geometric-boundary.ts";

export interface BinaryValue {
  readonly coefficient: bigint;
  readonly exponent: number;
}

// Decode a finite double exactly, including subnormals. Inputs were validated upstream.
function binaryValue(value: number): BinaryValue {
  const bytes = new DataView(new ArrayBuffer(8));
  bytes.setFloat64(0, value);
  const bits = bytes.getBigUint64(0);
  const exponentBits = Number((bits >> 52n) & 0x7ffn);
  const fraction = bits & ((1n << 52n) - 1n);
  const sign = value < 0 ? -1n : 1n;
  return {
    coefficient: sign * (exponentBits === 0 ? fraction : fraction + (1n << 52n)),
    exponent: exponentBits === 0 ? -1074 : exponentBits - 1075,
  };
}

function exactSum(values: ReadonlyArray<BinaryValue>): BinaryValue {
  const exponent = values.reduce((smallest, value) => Math.min(smallest, value.exponent), 0);
  const coefficient = values.reduce((total, value) =>
    total + (value.coefficient << BigInt(value.exponent - exponent)), 0n);
  return { coefficient, exponent };
}

function compareShifted(left: bigint, right: bigint, shift: number): boolean {
  return shift >= 0 ? left >= (right << BigInt(shift)) : (left << BigInt(-shift)) >= right;
}

function roundedQuotient(numerator: bigint, denominator: bigint, shift: number): bigint {
  const dividend = shift >= 0 ? numerator << BigInt(shift) : numerator;
  const divisor = shift >= 0 ? denominator : denominator << BigInt(-shift);
  const quotient = dividend / divisor;
  const remainder = dividend % divisor;
  const comparison = 2n * remainder - divisor;
  return comparison > 0n || (comparison === 0n && quotient % 2n === 1n) ? quotient + 1n : quotient;
}

function binaryRatio(numerator: BinaryValue, denominator: BinaryValue): number {
  if (numerator.coefficient === 0n) {
    return 0;
  }
  const sign = numerator.coefficient < 0n ? -1 : 1;
  const magnitude = numerator.coefficient < 0n ? -numerator.coefficient : numerator.coefficient;
  const shift = numerator.exponent - denominator.exponent;
  let exponent = magnitude.toString(2).length - denominator.coefficient.toString(2).length;
  if (!compareShifted(magnitude, denominator.coefficient, exponent)) {
    exponent -= 1;
  }
  const unitExponent = Math.max(-1074, exponent + shift - 52);
  const significand = roundedQuotient(magnitude, denominator.coefficient, shift - unitExponent);
  return sign * Number(significand) * 2 ** unitExponent;
}

// Exact binary accumulation prevents loss of tiny weights before a large value
// can compensate for them. Only the final normalized mean rounds to a double.
export function weightedMean(weights: ReadonlyArray<number>, values: ReadonlyArray<number>): number {
  const binaryWeights = weights.map(binaryValue);
  const products = values.map((value, index) => {
    const binary = binaryValue(value);
    const weight = binaryWeights[index];
    return {
      coefficient: weight.coefficient * binary.coefficient,
      exponent: weight.exponent + binary.exponent,
    };
  });
  return binaryRatio(exactSum(products), exactSum(binaryWeights));
}

export function geometricSpeedup(
  weights: ReadonlyArray<number>, baselines: ReadonlyArray<number>, candidates: ReadonlyArray<number>,
): number {
  const logarithms = baselines.map((baseline, index) => Math.log(baseline) - Math.log(candidates[index]));
  const meanLog = weightedMean(weights, logarithms);
  // Resolve the final binary binade against MAX using exact-input log bounds.
  // Rounded separate logs alone can cross overflow for a finite true score.
  if (meanLog >= Math.log(Number.MAX_VALUE / 2)) {
    return upperBoundaryGeometricMean(weights.map(binaryValue), baselines.map(binaryValue), candidates.map(binaryValue));
  }
  // Include a guard inside the first normal binade so log rounding cannot
  // bypass exact subnormal rounding or reach the quantizer's upper endpoint.
  if (meanLog <= Math.log(2 ** -1022) + Math.LN2 / 2) {
    return lowerBoundaryGeometricMean(weights.map(binaryValue), baselines.map(binaryValue), candidates.map(binaryValue));
  }
  return Math.exp(meanLog);
}
