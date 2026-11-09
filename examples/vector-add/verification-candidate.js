// An offline evaluator check, not a model-generated optimization result.
function vectorAdd(a, b) {
  const output = new Float32Array(a.length);
  for (let index = 0; index < a.length; index += 1) {
    output[index] = a[index] + b[index];
  }
  return output;
}
