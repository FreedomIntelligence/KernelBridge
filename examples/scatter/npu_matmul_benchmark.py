"""Frozen Cube correctness, original F timing and explicit eager ABBA qualification.

This driver supplies no candidate implementation. Each invocation owns one fresh
job directory; recover its receipt after interruption instead of running it again.
"""
import argparse
import ast
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import statistics
import sys
import time
import traceback

from npu_matmul_eager import matmul_eager
from npu_candidate_audit import audit_wrapper, audit_native_binding
from npu_diagnostics import collect_ir, validated_ir_artifacts
try:
    from diagnostic_policy import assess_diagnostics, diagnostic_reason
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'operator_pipeline'))
    from diagnostic_policy import assess_diagnostics, diagnostic_reason

FIELDS = {'project_json_src', 'host_tiling_src', 'host_operator_src', 'kernel_src', 'python_bind_src', 'model_src'}
SEEDS = (1024, 1025, 1026, 1027, 1028)
CASE_ID = 'matmul-fp16-128x128x128'
PROTOCOL = 'cube-fp16-128-eager-abba-12x20-v1'
CONTRACT = {
    'task_id': 'cann-cube-matmul-fp16-128', 'case_id': CASE_ID,
    'shape': [128, 128, 128], 'dtype': 'float16', 'accumulator_dtype': 'float32',
    'seeds': list(SEEDS), 'distribution': 'standard_normal', 'atol': 0.01, 'rtol': 0.01,
    'F': {'warmup': 3, 'samples': 100, 'timer': 'npu_event', 'timing_seed': 1024},
    'G': {'protocol': PROTOCOL, 'warmup': 3, 'samples_per_arm': 20, 'blocks': 12,
          'order': ['eager', 'candidate', 'candidate', 'eager'],
          'seed_schedule': '1024 + block % 5', 'timer': 'npu_event',
          'aggregation': 'paired_block_log_mean', 'require_noise': True,
          'calibration': 'independent eager/eager AAAA with identical blocks and sample counts'},
}


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def canonical_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def sources(path):
    result = {}
    for node in ast.parse(Path(path).read_text()).body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
            raise ValueError('Candidate must contain six literal source assignments')
        name = node.targets[0].id
        if name not in FIELDS or name in result:
            raise ValueError('Unexpected/duplicate source assignment')
        result[name] = ast.literal_eval(node.value)
        if not isinstance(result[name], str):
            raise ValueError('Source must be a string')
    if set(result) != FIELDS:
        raise ValueError('Six source fields required')
    return result


def audit_sources(parsed):
    report = audit_wrapper(parsed['model_src'])
    report.update(audit_native_binding(parsed['python_bind_src']))
    # These are source screens, not a claim that static text proves hardware use.
    native = '\n'.join(parsed[name] for name in ('host_tiling_src', 'host_operator_src', 'kernel_src', 'python_bind_src'))
    native = re.sub(r'//[^\n]*|/\*.*?\*/|"(?:\\.|[^"\\])*"', '', native, flags=re.S)
    if re.search(r'\b(?:at|torch)::(?:matmul|mm|bmm|addmm|einsum)\s*\(|\baclnn(?:Matmul|Mm|Gemm)(?:GetWorkspaceSize)?\s*\(|\.\s*cpu\s*\(', native):
        raise ValueError('Candidate may not delegate matmul to Torch/ACLNN or CPU')
    kernel = re.sub(r'//[^\n]*|/\*.*?\*/|"(?:\\.|[^"\\])*"', '', parsed['kernel_src'], flags=re.S)
    if re.search(r'\.\s*(?:GetValue|SetValue)\s*\(', kernel):
        raise ValueError('Scalar tensor simulation is outside the native Cube task')
    if not re.search(r'\b(?:IterateAll|Iterate|Mmad)\s*\(', kernel):
        raise ValueError('Native Cube/Matmul computation is required')
    return {**report, 'native_cube_source_screen': 'passed', 'scope': 'static audit; retained build and profiler supply runtime evidence'}


def suite(torch, device):
    cases = []
    for seed in SEEDS:
        generator = torch.Generator(device='cpu').manual_seed(seed)
        a = torch.randn((128, 128), generator=generator, dtype=torch.float16)
        b = torch.randn((128, 128), generator=generator, dtype=torch.float16)
        oracle = torch.matmul(a.float(), b.float()).to(torch.float16)
        identity = hashlib.sha256(a.numpy().tobytes() + b.numpy().tobytes()).hexdigest()
        cases.append({'seed': seed, 'input_sha256': identity, 'a': a.to(device), 'b': b.to(device), 'oracle': oracle,
                      'input_a': a, 'input_b': b})
    return cases


def check(function, cases, torch, device):
    checks = []
    for case in cases:
        got = function(case['a'], case['b'])
        torch.npu.synchronize(device=device)
        if not isinstance(got, torch.Tensor) or got.device != device or got.dtype != torch.float16 or tuple(got.shape) != (128, 128) or not got.is_contiguous():
            raise ValueError(f'Output contract failed for seed {case["seed"]}')
        cpu = got.detach().cpu()
        if not torch.isfinite(cpu).all().item() or not torch.allclose(cpu, case['oracle'], atol=0.01, rtol=0.01):
            raise ValueError(f'FP32 oracle mismatch for seed {case["seed"]}')
        if not torch.equal(case['a'].cpu(), case['input_a']) or not torch.equal(case['b'].cpu(), case['input_b']):
            raise ValueError(f'Input mutation for seed {case["seed"]}')
        checks.append({'seed': case['seed'], 'input_sha256': case['input_sha256'], 'correct': True,
                       'max_abs_error': (cpu.float() - case['oracle'].float()).abs().max().item()})
    return checks


def measure(function, torch, device, *, samples, warmup=3):
    """Original F NPU Event method; retain every sample, without outlier removal."""
    elapsed = []
    with torch.npu.device(device):
        stream = torch.npu.current_stream(device)
        for _ in range(warmup):
            function()
            torch.npu.synchronize(device=device)
        for _ in range(samples):
            start, end = torch.npu.Event(enable_timing=True), torch.npu.Event(enable_timing=True)
            start.record(stream)
            function()
            end.record(stream)
            torch.npu.synchronize(device=device)
            value = start.elapsed_time(end) * 1000
            if not math.isfinite(value) or value <= 0:
                raise ValueError('NPU Event returned a nonpositive/nonfinite duration')
            elapsed.append(value)
    return {'us': statistics.mean(elapsed), 'samples_us': elapsed}


def paired(functions, cases, torch, device, *, pairs=12, repeats=20, warmup=3, checkpoint=None,
           order=('eager', 'candidate', 'candidate', 'eager'), on_phase=None):
    arms = []
    for pair in range(pairs):
        case = cases[pair % len(cases)]
        for position, name in enumerate(order):
            if on_phase is not None: on_phase('correctness')
            subchecks = check(functions[name], cases, torch, device)
            if on_phase is not None: on_phase('measurement')
            sample = measure(lambda: functions[name](case['a'], case['b']), torch, device, samples=repeats, warmup=warmup)
            arms.append({'pair': pair, 'position': position, 'arm': name, 'seed': case['seed'], 'input_sha256': case['input_sha256'],
                         'correctness': subchecks, **sample})
            if checkpoint is not None:
                checkpoint(arms)
    return arms


def save_json(path, value):
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(value, indent=2, allow_nan=False) + '\n')
    temp.replace(path)


def run(args):
    if (args.warmup, args.repeats, args.pairs) != (3, 20, 12):
        raise ValueError('Frozen Cube measurement requires warmup=3, repeats=20, pairs=12')
    job = args.output.resolve()
    job.mkdir(parents=True, exist_ok=False)
    source_sha = digest(args.candidate)
    shutil.copyfile(args.candidate, job / 'candidate.py')
    for name in ('npu_matmul_benchmark.py', 'npu_matmul_eager.py', 'npu_diagnostics.py', 'npu_candidate_audit.py'):
        shutil.copyfile(Path(__file__).with_name(name), job / name)
    request = {'mode': args.mode, 'source_sha256': source_sha, 'device': args.device, 'soc': args.soc,
               'warmup': args.warmup, 'repeats': args.repeats, 'pairs': args.pairs, 'profile': bool(args.profile),
               'baseline': 'torch_npu_eager_matmul', 'baseline_sha256': digest(Path(__file__).with_name('npu_matmul_eager.py')),
               'driver_sha256': digest(__file__), 'candidate_audit_sha256': digest(Path(__file__).with_name('npu_candidate_audit.py')), 'diagnostic_driver_sha256': digest(Path(__file__).with_name('npu_diagnostics.py')),
               'protocol': PROTOCOL, 'protocol_id': PROTOCOL, 'contract': CONTRACT, 'contract_sha256': canonical_digest(CONTRACT)}
    save_json(job / 'request.json', request)
    result = {'status': 'running', 'correct': False, **request, 'started_at_ns': time.time_ns(),
              'build_completed': False, 'phase': 'setup', 'cases': []}
    save_json(job / 'status.json', result)
    policy = {'on_failure': True, 'on_regression': True, 'required': bool(args.profile), 'scope': 'npu_diagnostic'}
    runtime = None
    compiler = None
    profiler_error = None
    phase = 'setup'
    def set_phase(value):
        nonlocal phase
        phase = value
    try:
        import torch
        import torch_npu
        from kernelcat_executor.backends.ascend.compiler import compile_sources
        from kernelcat_executor.backends.ascend.runtime import AscendRuntime
        from kernelcat_executor.build.inputs import BuildSettings
        import kernelcat_executor.backends.ascend.compiler as compiler

        phase = 'source_validation'
        parsed = sources(job / 'candidate.py')
        result['audit'] = audit_sources(parsed)
        phase = 'compile'
        build = job / 'build'
        shutil.copytree(Path(compiler.__file__).parent / 'templates/CppExtension', build / 'CppExtension')
        settings = BuildSettings('matmul', build, args.soc, args.device)
        compiled = compile_sources(parsed, settings, diagnostic_options=('-save-temps', '--save-temp-files'))
        phase = 'build_record'
        result['build_completed'] = True
        result['build_outputs'] = [{'path': str(item.path), 'sha256': item.sha256} for item in compiled.files]
        result['build_source_digest'] = compiled.source_digest
        result['build_target'] = compiled.target
        result['phase'] = phase = 'load'
        save_json(job / 'status.json', result)
        runtime = AscendRuntime(settings)
        runtime.load(compiled, parsed['model_src'])
        model = runtime.context['ModelNew']().eval().to(runtime.device)
        phase = 'input_preparation'
        cases = suite(torch, runtime.device)
        result['environment'] = {'torch': torch.__version__, 'torch_npu': torch_npu.__version__,
                                 'device': torch.npu.get_device_name(args.device), 'timer': 'npu_event'}
        with torch.no_grad():
            phase = 'correctness'
            subchecks = {'candidate': check(model, cases, torch, runtime.device), 'eager': check(matmul_eager, cases, torch, runtime.device)}
            first = cases[0]
            phase = 'measurement'
            result['F'] = {'correctness': subchecks, 'seed': first['seed'], 'input_sha256': first['input_sha256'],
                           'warmup': 3, 'samples': 100,
                           **measure(lambda: model(first['a'], first['b']), torch, runtime.device, samples=100)}
            save_json(job / 'F.json', result['F'])
            input_sha = canonical_digest([case['input_sha256'] for case in cases])
            noise_arms = paired({'eager': matmul_eager}, cases, torch, runtime.device,
                                order=('eager',) * 4,
                                checkpoint=lambda rows: save_json(job / 'noise-arms.json', rows), on_phase=set_phase)
            noise = {key: request[key] for key in ('pairs', 'warmup', 'repeats', 'device', 'soc',
                                                  'baseline_sha256', 'driver_sha256', 'source_sha256', 'contract_sha256')}
            noise.update(schema_version='kernelbridge.noise-calibration/v1', protocol='independent_eager_aaaa',
                         environment=result['environment'], cases=[{'id': CASE_ID, 'input_sha256': input_sha, 'arms': noise_arms}])
            save_json(job / 'noise.json', noise)
            result.update(noise_calibration=noise, noise_calibration_sha256=digest(job / 'noise.json'))
            arms = paired({'eager': matmul_eager, 'candidate': model}, cases, torch, runtime.device,
                          checkpoint=lambda rows: save_json(job / 'arms.json', rows), on_phase=set_phase)
            speeds = []
            for pair in range(args.pairs):
                block = arms[4 * pair:4 * pair + 4]
                speeds.append(statistics.mean([block[0]['us'], block[3]['us']]) / statistics.mean([block[1]['us'], block[2]['us']]))
            values = {name: math.exp(statistics.mean(math.log(statistics.mean(
                      row['us'] for row in arms[4 * pair:4 * pair + 4] if row['arm'] == name)) for pair in range(args.pairs)))
                      for name in ('eager', 'candidate')}
            row = {'id': CASE_ID, 'input_sha256': input_sha,
                   'correct': True, 'subchecks': subchecks, 'arms': arms, 'pair_speedups': speeds,
                   **values, 'speedup': values['eager'] / values['candidate']}
            result.update(status='completed', correct=True, phase='completed', cases=[row], geomean_speedup=row['speedup'])
            save_json(job / 'measurements.json', {'F': result['F'], 'cases': result['cases']})
            if diagnostic_reason(policy, failed=False, regression=row['speedup'] <= 1):
                phase = 'profiler'
                try:
                    with torch_npu.profiler.profile(activities=[torch_npu.profiler.ProfilerActivity.CPU, torch_npu.profiler.ProfilerActivity.NPU],
                            schedule=torch_npu.profiler.schedule(wait=0, warmup=0, active=1, repeat=1),
                            record_shapes=True, on_trace_ready=torch_npu.profiler.tensorboard_trace_handler(str(job / 'profile'))) as profiler:
                        with torch.profiler.record_function('kernelbridge_eager'):
                            matmul_eager(first['a'], first['b'])
                        with torch.profiler.record_function('kernelbridge_candidate'):
                            model(first['a'], first['b'])
                        torch.npu.synchronize(device=runtime.device)
                        profiler.step()
                except Exception as error:
                    profiler_error = str(error)
    except Exception as error:
        result.update(status='failed', correct=False, phase=phase, failure_stage=phase,
                      error=type(error).__name__, message=str(error), traceback=traceback.format_exc())
    finally:
        if runtime is not None:
            try:
                runtime.close()
            except Exception as error:
                if result['status'] == 'completed':
                    result.update(status='failed', correct=False, phase='cleanup', failure_stage='cleanup')
                result['cleanup_error'] = str(error)
    try:
        if compiler is None:
            import kernelcat_executor.backends.ascend.compiler as compiler
        build_diagnostics = compiler.collect_build_diagnostics(job / 'build')
    except Exception as error:
        # A diagnostics failure must not replace the original compile/runtime result.
        build_diagnostics = {'schema_version': 'kernelbridge.native-build-diagnostics/v1',
                             'status': 'unavailable', 'source_digest': result.get('build_source_digest'),
                             'target': result.get('build_target'), 'steps': [], 'artifacts': {},
                             'reason': 'Build diagnostic collection failed: ' + str(error)}
    result['build_diagnostics'] = {**build_diagnostics, 'candidate_sha256': source_sha}
    triggered = diagnostic_reason(policy, failed=result['status'] != 'completed',
                                  regression=any(row['speedup'] <= 1 for row in result['cases']))
    ir_error = None
    ir_attempts = []
    if triggered:
        try:
            ir_attempts = collect_ir(job)
            if not ir_attempts:
                ir_error = 'Original build produced no CANN compiler recipe'
            elif any(record.get('status') != 'completed' for record in ir_attempts):
                ir_error = 'Some CANN IR recipes did not complete; inspect original ir receipts'
        except Exception as error:
            ir_error = str(error)
            try:
                retained = json.loads((job / 'ir/receipt.json').read_text())
                if isinstance(retained, list): ir_attempts = retained
            except (OSError, ValueError):
                pass
    try:
        ir_artifacts = validated_ir_artifacts(job, ir_attempts)
    except Exception as error:
        ir_artifacts = {}
        ir_error = 'IR artifact validation failed: ' + str(error)
    records = []
    for kind, paths in [('ir', []),
                        ('profiler', list((job / 'profile').rglob('kernel_details.csv')))]:
        artifacts = ir_artifacts if kind == 'ir' else {
            str(p.relative_to(job)): digest(p) for p in paths if p.is_file() and p.stat().st_size}
        reason = ir_error if kind == 'ir' else profiler_error
        partial = bool(reason) and bool(artifacts)
        record = {'kind': kind, 'status': 'failed' if partial else 'completed' if artifacts else 'unavailable', 'scope': policy['scope'],
                  'candidate_sha256': source_sha, 'artifacts': artifacts}
        if partial or not artifacts:
            record['partial'] = partial
            record['reason'] = reason or 'No original artifact; failed candidates are not executed for diagnostics'
        if kind == 'ir':
            record['attempt_evidence'] = {str(p.relative_to(job)): digest(p) for p in (job / 'ir').rglob('*')
                                          if p.suffix in {'.json', '.log'} and p.is_file() and p.stat().st_size}
        records.append(record)
    result['diagnostics'] = assess_diagnostics(policy, result, records, job, source_sha,
                                               binding=request, handle={'pid': os.getpid(), 'job': str(job)})
    result['ended_at_ns'] = time.time_ns()
    save_json(job / 'result.json', result)
    save_json(job / 'status.json', {key: result[key] for key in (
        'status', 'correct', 'source_sha256', 'build_completed', 'phase', 'ended_at_ns')})
    print(json.dumps({key: value for key, value in result.items() if key not in ('cases', 'F', 'noise_calibration', 'build_outputs', 'traceback')}), flush=True)
    return result['status'] == 'completed'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidate', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--mode', choices=['matmul'], required=True)
    parser.add_argument('--device', type=int, default=1)
    parser.add_argument('--soc', default='ai_core-Ascend910_9382')
    parser.add_argument('--warmup', type=int, default=3)
    parser.add_argument('--repeats', type=int, default=20)
    parser.add_argument('--pairs', type=int, default=12)
    parser.add_argument('--profile', action='store_true')
    return 0 if run(parser.parse_args()) else 1


if __name__ == '__main__':
    raise SystemExit(main())
