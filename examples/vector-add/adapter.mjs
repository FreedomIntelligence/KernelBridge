/** Finite CPU vector-add evaluator. Node VM bounds execution, not OS permissions. */
import {createHash} from 'node:crypto';
import {readFile} from 'node:fs/promises';
import {createRequire} from 'node:module';
import {cpus} from 'node:os';
import {join} from 'node:path';
import {pathToFileURL} from 'node:url';
import {types} from 'node:util';
import vm from 'node:vm';

const runtime = JSON.parse(await readFile(process.argv[2], 'utf8'));
const require = createRequire(join(runtime.agent_root, 'package.json'));
const ts = require('typescript');
const {canonicalKey} = await import(pathToFileURL(join(runtime.agent_root, 'src/run/scoring/canonical.ts')));
const {wireValidator} = await import(pathToFileURL(join(runtime.agent_root, 'src/clients/wire-schemas.ts')));
const {SCHEMA} = await import(pathToFileURL(join(runtime.agent_root, 'src/generated/rsi.ts')));
const validate = wireValidator(SCHEMA.$id, 'AdapterRequest');
const hash = value => createHash('sha256').update(canonicalKey(value)).digest('hex');
const protocol = 'cpu-vector-add-f32/paired-ns/v1';
const sizes = {measure: [1024, 8192, 65536], confirm: [2048, 16384, 131072]};
const pairs = 12;
const repetitions = 8;

function parse(files) {
  if (files.length !== 1 || files[0].path !== 'solution.js') {
    throw new Error('vector-add requires exactly solution.js');
  }
  return ts.createSourceFile('solution.js', files[0].text, ts.ScriptTarget.ES2022, true, ts.ScriptKind.JS);
}

function complexity(files) {
  const result = {nodes: 0, branches: 0, specializations: 0, cognitive: 0, configuration: 0};
  function visit(node, depth) {
    result.nodes += 1;
    const branch = ts.isIfStatement(node) || ts.isIterationStatement(node, false)
      || ts.isConditionalExpression(node) || ts.isCaseClause(node);
    if (branch) {
      result.branches += 1;
      result.cognitive += depth + 1;
    }
    ts.forEachChild(node, child => visit(child, depth + Number(branch)));
  }
  visit(parse(files), 0);
  return result;
}

function decompose(request) {
  parse(request.baseline);
  parse(request.keep);
  const before = request.baseline[0].text;
  const after = request.keep[0].text;
  const units = before === after ? [] : [{
    unit_id: `program:${hash(request.keep).slice(0, 16)}`, anchor: 'SourceFile:solution.js',
    path: 'solution.js', before, after, dependencies: ['solution.js'], ku_ids: [],
  }];
  return {units, candidate_complexity: complexity(request.keep), baseline_complexity: complexity(request.baseline)};
}

function inputSeed(namespace, index) {
  return createHash('sha256').update(`${namespace}:${index}`).digest().readUInt32LE(0) || 1;
}

function inputs(seed, length) {
  let state = seed;
  function next() {
    state ^= state << 13;
    state ^= state >>> 17;
    state ^= state << 5;
    return Math.fround(((state >>> 0) / 4294967296 - 0.5) * 2000);
  }
  return {a: Array.from({length}, next), b: Array.from({length}, next)};
}

function evaluator(files) {
  parse(files);
  const context = vm.createContext(Object.create(null), {codeGeneration: {strings: false, wasm: false}});
  new vm.Script(`${files[0].text}\nif (typeof vectorAdd !== 'function') throw new Error('vectorAdd required');`)
    .runInContext(context, {timeout: 500});
  const prepare = new vm.Script('a = new Float32Array(JSON.parse(input).a); b = new Float32Array(JSON.parse(input).b);');
  const invoke = new vm.Script('vectorAdd(a, b)');
  const timed = new vm.Script(`for (let repeat = 0; repeat < ${repetitions}; repeat += 1) result = vectorAdd(a, b);`);
  return {
    prepare(sample) {
      context.input = JSON.stringify(sample);
      prepare.runInContext(context, {timeout: 1000});
    },
    correct(sample) {
      const a = context.a;
      const b = context.b;
      const output = invoke.runInContext(context, {timeout: 1000});
      if (!types.isFloat32Array(output) || output.length !== sample.a.length) return false;
      for (let index = 0; index < sample.a.length; index += 1) {
        if (a[index] !== sample.a[index] || b[index] !== sample.b[index]
            || output[index] !== Math.fround(sample.a[index] + sample.b[index])) return false;
      }
      return true;
    },
    time() {
      const start = process.hrtime.bigint();
      timed.runInContext(context, {timeout: 2000});
      const elapsed = Number(process.hrtime.bigint() - start);
      return Math.max(1, Math.round(elapsed / repetitions));
    },
  };
}

function initialize(files, namespace) {
  let run;
  try {
    run = evaluator(files);
    for (const length of [0, 1, 3, 17, 257, 65536]) {
      const sample = inputs(inputSeed(`${namespace}:correctness`, length), length);
      run.prepare(sample);
      if (!run.correct(sample)) return {run: null, correct: false};
    }
  } catch {
    return {run: null, correct: false};
  }
  return {run, correct: true};
}

function timedPair(arms, sample, index) {
  const row = {keep: 0, remove: 0};
  for (const name of index % 2 === 0 ? ['keep', 'remove'] : ['remove', 'keep']) {
    const arm = arms[name];
    if (!arm.correct) continue;
    try {
      arm.run.prepare(sample);
      if (!arm.run.correct(sample)) throw new Error('timed input failed correctness');
      arm.run.time(); // Fixed warmup; never select a stopping point from timings.
      row[name] = arm.run.time();
    } catch {
      arm.correct = false;
    }
  }
  return row;
}

function measured(request) {
  const confirmation = request.operation === 'confirm';
  const arms = {
    keep: initialize(request.keep, request.sample_namespace),
    remove: initialize(request.remove, request.sample_namespace),
  };
  const samples = [];
  for (let index = 0; index < pairs; index += 1) {
    const length = sizes[confirmation ? 'confirm' : 'measure'][index % 3];
    const seed = inputSeed(request.sample_namespace, index);
    const timing = timedPair(arms, inputs(seed, length), index);
    samples.push({sample_id: `${request.sample_namespace}/pair/${index}`, length, seed, ...timing});
  }
  const features = name => ({surface: ['Float32Array'], lowered: null,
    runtime: arms[name].correct ? ['f32-exact', 'inputs-preserved'] : []});
  const evidence = {protocol, timing_unit: 'ns', repetitions, samples};
  return {measurement: {
    measurement_id: `cpu:${hash(request)}`, task_id: request.task_id,
    environment_id: `node:${process.version}:${process.platform}:${process.arch}:${hash(cpus().map(cpu => cpu.model)).slice(0, 16)}`,
    protocol_id: protocol, keep_digest: hash(request.keep), remove_digest: hash(request.remove),
    sample_ids: samples.map(sample => sample.sample_id),
    keep_log_scores: samples.map(sample => sample.keep > 0 ? -Math.log(sample.keep) : 0),
    remove_log_scores: samples.map(sample => sample.remove > 0 ? -Math.log(sample.remove) : 0),
    keep_correct: arms.keep.correct, remove_correct: arms.remove.correct,
    independent_confirmation: confirmation,
    evidence_ids: [`cases:${hash(samples.map(({sample_id, length, seed}) => ({sample_id, length, seed})))}`,
      `timings:${hash(evidence)}`,
      ...samples.map(sample => `ns:${sample.sample_id}:n=${sample.length}:keep=${sample.keep}:remove=${sample.remove}:reps=${repetitions}`)],
    features: features('keep'), remove_features: features('remove'),
    visible_coverage: confirmation ? [] : ['float32', 'lengths:1024,8192,65536', 'seeded-finite-inputs'],
    holdout_coverage: confirmation ? ['independent-namespace', 'lengths:2048,16384,131072', 'float32-exact'] : null,
    keep_complexity: complexity(request.keep), remove_complexity: complexity(request.remove),
  }, timing: evidence};
}

let raw = '';
for await (const chunk of process.stdin) {
  raw += chunk;
  if (Buffer.byteLength(raw) > 4 * 1024 * 1024) throw new Error('adapter input exceeds 4 MiB');
}
const action = JSON.parse(raw);
if (!validate(action.arguments)) throw new Error('invalid original RSI AdapterRequest');
const request = action.arguments;
const result = request.operation === 'decompose' ? {decomposition: decompose(request)} : measured(request);
const {timing, ...rsi} = result;
process.stdout.write(JSON.stringify({status: 'completed', data: {rsi, ...(timing ? {timing} : {})}}));
