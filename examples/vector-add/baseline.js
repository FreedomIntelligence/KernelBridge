// Deliberately allocation-heavy, correct CPU teaching baseline.
function vectorAdd(a, b) {
  const pairs = Array.from(a, (value, index) => [value, b[index]]);
  const sums = pairs.map(pair => pair[0] + pair[1]);
  return new Float32Array(sums);
}
