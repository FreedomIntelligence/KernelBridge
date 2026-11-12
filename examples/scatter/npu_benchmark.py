"""One frozen native candidate, CPU oracle and paired same-device NPU eager timing.

Each invocation owns a fresh output directory. A prior invocation is recovered
from its result and process handle, never re-executed under the same identity.
"""
import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import shutil
import statistics
import sys
import time
import traceback

from npu_eager import scatter_eager
from npu_diagnostics import collect_ir
try:
    from diagnostic_policy import assess_diagnostics, diagnostic_reason
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'operator_pipeline'))
    from diagnostic_policy import assess_diagnostics, diagnostic_reason

FIELDS = {'project_json_src', 'host_tiling_src', 'host_operator_src', 'kernel_src', 'python_bind_src', 'model_src'}


def digest(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def sources(path):
    result = {}
    for node in ast.parse(path.read_text()).body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
            raise ValueError('Candidate must contain six literal source assignments')
        name = node.targets[0].id
        if name not in FIELDS or name in result:
            raise ValueError('Unexpected/duplicate source assignment')
        result[name] = ast.literal_eval(node.value)
        if not isinstance(result[name], str): raise ValueError('Source must be a string')
    if set(result) != FIELDS: raise ValueError('Six source fields required')
    return result


def suite(torch, mode, device):
    # All cases use the frozen bins=33 contract; inputs are prepared outside timing.
    configurations = [(1, 1, 'sparse'), (17, 7, 'sparse'), (129, 65, 'uniform'),
                      (256, 128, 'collision'), (33, 257, 'sorted'), (513, 32, 'hot')]
    result = []
    for number, (rows, features, family) in enumerate(configurations):
        generator = torch.Generator().manual_seed(11011 + number)
        src = torch.randint(-4096, 4097, (rows, features), generator=generator).float() / 256
        index = torch.randint(0, 33, (rows,), generator=generator)
        if family == 'collision': index.zero_()
        elif family == 'sorted': index = index.sort().values
        elif family == 'hot': index.remainder_(4)
        expected = torch.zeros((33, features), dtype=src.dtype)
        for bin_id in range(33):
            selected = src[index == bin_id]
            if len(selected): expected[bin_id] = selected.amax(0) if mode == 'max' else selected.amin(0)
        identity = hashlib.sha256(src.numpy().tobytes() + index.numpy().tobytes()).hexdigest()
        result.append({'id': f'{family}-{rows}x{features}', 'input_sha256': identity,
                       'src': src.to(device), 'index': index.to(device), 'expected': expected})
    return result


def measure(function, torch, warmup, repeats):
    for _ in range(warmup): function()
    torch.npu.synchronize()
    start = time.perf_counter_ns()
    for _ in range(repeats): function()
    torch.npu.synchronize()
    return (time.perf_counter_ns() - start) / repeats / 1000


def run(args):
    import torch
    import torch_npu
    from kernelcat_executor.backends.ascend.compiler import compile_sources
    from kernelcat_executor.backends.ascend.runtime import AscendRuntime
    from kernelcat_executor.build.inputs import BuildSettings
    import kernelcat_executor.backends.ascend.compiler as compiler

    job = args.output.resolve(); job.mkdir(parents=True, exist_ok=False)
    candidate = args.candidate.resolve(); source_sha = digest(candidate)
    shutil.copyfile(candidate, job / 'candidate.py')
    shutil.copyfile(__file__, job / 'driver.py')
    shutil.copyfile(Path(__file__).with_name('npu_eager.py'), job / 'npu_eager.py')
    shutil.copyfile(Path(__file__).with_name('npu_diagnostics.py'), job / 'npu_diagnostics.py')
    request = {'mode': args.mode, 'source_sha256': source_sha, 'device': args.device, 'soc': args.soc,
               'warmup': args.warmup, 'repeats': args.repeats, 'pairs': args.pairs,
               'baseline': 'torch_npu_eager_broadcast_mask_reduce', 'baseline_sha256': digest(Path(__file__).with_name('npu_eager.py')),
               'driver_sha256': digest(__file__), 'diagnostic_driver_sha256': digest(Path(__file__).with_name('npu_diagnostics.py')),
               'profile': bool(args.profile)}
    (job / 'request.json').write_text(json.dumps(request, indent=2) + '\n')
    status = {'status': 'running', **request, 'started_at_ns': time.time_ns()}
    policy = {'on_failure': True, 'on_regression': True, 'required': bool(args.profile), 'scope': 'npu_diagnostic'}
    profiler_error = None
    (job / 'status.json').write_text(json.dumps(status))
    try:
        parsed = sources(job / 'candidate.py')
        build = job / 'build'
        shutil.copytree(Path(compiler.__file__).parent / 'templates/CppExtension', build / 'CppExtension')
        settings = BuildSettings('scatter_' + args.mode, build, args.soc, args.device)
        compiled = compile_sources(parsed, settings, diagnostic_options=('-save-temps', '--save-temp-files'))
        runtime = AscendRuntime(settings); runtime.load(compiled, parsed['model_src'])
        model = runtime.context['ModelNew']().eval()
        cases = suite(torch, args.mode, runtime.device)
        timings = []
        with torch.no_grad():
            for case in cases:
                candidate_call = lambda: model(case['src'], case['index'])
                baseline_call = lambda: scatter_eager(case['src'], case['index'], 33, args.mode)
                got = candidate_call(); eager = baseline_call(); torch.npu.synchronize()
                if got.device != runtime.device or eager.device != runtime.device:
                    raise ValueError('Result left the measured NPU')
                if not torch.equal(got.cpu(), case['expected']):
                    raise ValueError('Candidate correctness failed: ' + case['id'])
                if not torch.equal(eager.cpu(), case['expected']):
                    raise ValueError('Eager correctness failed: ' + case['id'])
                arms = []
                for pair in range(args.pairs):
                    for name, fn in [('eager', baseline_call), ('candidate', candidate_call), ('candidate', candidate_call), ('eager', baseline_call)]:
                        arms.append({'pair': pair, 'arm': name, 'us': measure(fn, torch, args.warmup, args.repeats)})
                medians = {name: statistics.median(row['us'] for row in arms if row['arm'] == name) for name in ['eager', 'candidate']}
                timings.append({'id': case['id'], 'input_sha256': case['input_sha256'], 'correct': True, 'arms': arms,
                                **medians, 'speedup': medians['eager'] / medians['candidate']})
            if diagnostic_reason(policy, failed=False, regression=any(row['speedup'] <= 1 for row in timings)):
                case = cases[2]
                try:
                    with torch_npu.profiler.profile(activities=[torch_npu.profiler.ProfilerActivity.CPU, torch_npu.profiler.ProfilerActivity.NPU],
                            schedule=torch_npu.profiler.schedule(wait=0, warmup=0, active=1, repeat=1),
                            record_shapes=True, on_trace_ready=torch_npu.profiler.tensorboard_trace_handler(str(job / 'profile'))) as profiler:
                        with torch.profiler.record_function('kernelbridge_eager'):
                            scatter_eager(case['src'], case['index'], 33, args.mode)
                        with torch.profiler.record_function('kernelbridge_candidate'):
                            model(case['src'], case['index'])
                        torch.npu.synchronize(); profiler.step()
                except Exception as error:
                    profiler_error = str(error)
        result = {**status, 'status': 'completed', 'correct': True, 'cases': timings,
                  'geomean_speedup': statistics.geometric_mean(row['speedup'] for row in timings),
                  'environment': {'torch': torch.__version__, 'torch_npu': torch_npu.__version__, 'device': torch.npu.get_device_name(args.device)},
                  'build_outputs': [{'path': str(item.path), 'sha256': item.sha256} for item in compiled.files]}
        runtime.close()
    except Exception as error:
        result = {**status, 'status': 'failed', 'error': type(error).__name__, 'message': str(error), 'traceback': traceback.format_exc()}
    ir_error = None
    triggered = diagnostic_reason(policy, failed=result['status'] != 'completed',
                                  regression=any(row['speedup'] <= 1 for row in result.get('cases', [])))
    if triggered:
        try:
            ir_attempts = collect_ir(job)
            if not ir_attempts: ir_error = 'Original build produced no CANN compiler recipe from which to derive IR'
            elif any(record.get('returncode') != 0 for record in ir_attempts):
                ir_error = 'Pinned CANN compiler could not emit LLVM IR; inspect ir/receipt.json and compiler logs'
        except Exception as error:
            ir_error = str(error)
    records = []
    for kind, paths in [('ir', [p for p in (job / 'ir').rglob('*') if p.suffix in {'.ll', '.bc'}]),
                        ('profiler', list((job / 'profile').rglob('kernel_details.csv')))]:
        artifacts = {str(p.relative_to(job)): digest(p) for p in paths if p.is_file() and p.stat().st_size}
        if artifacts:
            records.append({'kind': kind, 'status': 'completed', 'scope': policy['scope'], 'candidate_sha256': source_sha, 'artifacts': artifacts})
        else:
            reason = profiler_error if kind == 'profiler' else ir_error
            evidence = {str(p.relative_to(job)): digest(p) for p in (job / 'ir').glob('*') if p.is_file() and p.stat().st_size} if kind == 'ir' else {}
            records.append({'kind': kind, 'status': 'unavailable', 'scope': policy['scope'], 'candidate_sha256': source_sha,
                            'attempt_evidence': evidence,
                            'reason': reason or 'Original attempt produced no diagnostic artifact; compile or correctness failure does not authorize executing a broken candidate.'})
    result['diagnostics'] = assess_diagnostics(policy, result, records, job, source_sha,
                                               binding=request, handle={'pid': os.getpid(), 'job': str(job)})
    result['ended_at_ns'] = time.time_ns()
    temp = job / 'result.tmp'; temp.write_text(json.dumps(result, indent=2, allow_nan=False) + '\n'); temp.replace(job / 'result.json')
    (job / 'status.json').write_text(json.dumps({key: result[key] for key in ('status', 'source_sha256', 'ended_at_ns')}))
    print(json.dumps({key: value for key, value in result.items() if key not in ('cases', 'build_outputs', 'traceback')}), flush=True)
    return result['status'] == 'completed'


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidate', type=Path, required=True); parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--mode', choices=['max', 'min'], required=True); parser.add_argument('--device', type=int, default=0)
    parser.add_argument('--soc', default='ai_core-Ascend910_9382')
    parser.add_argument('--warmup', type=int, default=3); parser.add_argument('--repeats', type=int, default=10)
    parser.add_argument('--pairs', type=int, default=3); parser.add_argument('--profile', action='store_true')
    raise SystemExit(0 if run(parser.parse_args()) else 1)
