"""Pinned opt-in RSI adapter for the native float32[128] vector-add acceptance fixture.

Run only in a provisioned isolated NPU environment. Each cached build retains its
original receipt; incomplete builds are never restarted. Timings alternate arms
in separate persistent processes so job-local libraries cannot alias each other.
"""
from __future__ import annotations
import argparse
import ast
import hashlib
import json
import math
import os
from pathlib import Path
import select
import signal
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'source/packages/executor/src'))


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def source(files):
    if len(files) != 1 or files[0]['path'] != 'solution.py':
        raise ValueError('native fixture requires exactly solution.py')
    return files[0]['text']


def complexity(files):
    # Python wrapper syntax only, not an estimate of lowered native complexity.
    tree = ast.parse(source(files))
    return dict(nodes=sum(1 for _ in ast.walk(tree)), branches=0,
                specializations=0, cognitive=0, configuration=0)


def build(files, args):
    from kernelcat_executor.evaluation.evaluate import run_job
    from kernelcat_executor.evaluation.protocols.six_source.config import Request
    root = args.state / 'builds' / digest(files)
    if root.exists():
        result = json.loads((root / 'job/result.json').read_text())
        if result['data']['candidate_sha256'] != hashlib.sha256(source(files).encode()).hexdigest():
            raise ValueError('cached source identity differs')
        return root, result
    root.mkdir(parents=True)
    candidate = root / 'candidate.py'
    candidate.write_text(source(files))
    result = run_job(Request(candidate, args.reference, root / 'job', 'audit_add', args.soc, args.device, True, 600))
    return root, result


def worker(root, device):
    from kernelcat_executor.backends.ascend.runtime import AscendRuntime
    from kernelcat_executor.build.inputs import BuildSettings
    from kernelcat_executor.build.outputs import BuildOutputs, BuiltFile
    from kernelcat_executor.evaluation.protocols.six_source.correctness import execute_template
    from kernelcat_executor.evaluation.measurement.ascend_events import time_execution_event_template
    import torch
    import torch_npu
    record = json.loads((root / 'job/result.json').read_text())
    manifest = record['data']['result']['build']
    workspace = root / 'job/build'
    runtime = AscendRuntime(BuildSettings('audit_add', workspace, manifest['target'], device))
    context = {}
    exec(compile((root / 'candidate.py').read_text(), '<frozen-candidate>', 'exec'), context)
    outputs = BuildOutputs(workspace, manifest['source_digest'], manifest['target'],
        tuple(BuiltFile(workspace / f['path'], f['sha256']) for f in manifest['files']))
    runtime.load(outputs, context['model_src'])
    exec(compile((root / 'job/reference.py').read_text(), '<frozen-reference>', 'exec'), runtime.context)
    torch_npu.npu.set_device(device)
    print(json.dumps({'ready': True, 'device': device, 'name': torch_npu.npu.get_device_name(device),
                     'torch': torch.__version__, 'torch_npu': torch_npu.__version__}), flush=True)
    try:
        for line in sys.stdin:
            request = json.loads(line)
            correct, info, stage = execute_template(torch_npu.npu.synchronize, runtime.device,
                runtime.context, num_correct_trials=1, seed_num=request['seed'])
            samples = time_execution_event_template(runtime.context, runtime.device,
                torch_npu.npu.synchronize, torch_npu.npu.Event, 'ModelNew', num_perf_trials=20, num_warmup=3) if correct else []
            print(json.dumps({'correct': correct, 'info': info, 'failure_stage': stage,
                              'samples_ms': samples}), flush=True)
    finally:
        runtime.close()


def receive(process, timeout=180):
    if not select.select([process.stdout], [], [], timeout)[0]:
        raise TimeoutError('original native timing worker did not reply')
    line = process.stdout.readline()
    if not line:
        raise RuntimeError('original native timing worker closed')
    return json.loads(line)


def measure(request, args, attempt):
    arms = {}; handles = []; samples = []; environments = []
    try:
        for name in ('keep', 'remove'):
            root, receipt = build(request[name], args)
            arms[name] = {'correct': receipt['status'] == 'completed', 'receipt': receipt, 'root': str(root)}
            if not arms[name]['correct']:
                continue
            env = os.environ.copy()
            env['LD_LIBRARY_PATH'] = str(root / 'job/build/opp/vendors/customize/op_api/lib') + os.pathsep + env.get('LD_LIBRARY_PATH', '')
            env['ASCEND_CUSTOM_OPP_PATH'] = str(root / 'job/build/opp/vendors/customize')
            log = (attempt / (name + '.worker.log')).open('wb');handles.append(log)
            process = subprocess.Popen([sys.executable, '-B', str(Path(__file__).resolve()), '--worker', str(root), str(args.device)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log, env=env, text=True, start_new_session=True)
            arms[name]['process'] = process
            try:
                environments.append(receive(process))
            except (TimeoutError, RuntimeError, ValueError) as error:
                arm = arms[name]
                arm['correct'] = False
                arm['runtime_error'] = str(error)
                # The owned process is reaped before reporting a finite failure.
                try: os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError: pass
                process.wait()

        for index in range(12):
            row = {'sample_id': request['sample_namespace'] + '/pair/' + str(index)}
            seed = int(hashlib.sha256(row['sample_id'].encode()).hexdigest()[:8], 16)
            row['seed'] = seed
            for name in (('keep', 'remove') if index % 2 == 0 else ('remove', 'keep')):
                arm = arms[name]
                if not arm['correct']:
                    row[name] = None
                    continue
                process = arm['process'];process.stdin.write(json.dumps({'seed': seed}) + '\n');process.stdin.flush()
                try:
                    response = receive(process)
                except (TimeoutError, RuntimeError, ValueError) as error:
                    arm['correct'] = False
                    arm['runtime_error'] = str(error)
                    row[name] = None
                    continue
                row[name] = response
                arm['correct'] = response['correct'] and len(response['samples_ms']) == 20 and all(math.isfinite(t) and t > 0 for t in response['samples_ms'])
            samples.append(row)
            (attempt / 'samples.json').write_text(json.dumps(samples, indent=2))
        evidence = {'protocol': 'ascend-add-f32-128-alternating-v1', 'samples': samples,
                    'environments': environments, 'builds': {n: a['receipt'] for n, a in arms.items()}}
        (attempt / 'evidence.json').write_text(json.dumps(evidence, indent=2))
        scores = lambda name: [-math.log(sum(row[name]['samples_ms']) / len(row[name]['samples_ms']))
                              if row[name] and row[name]['samples_ms'] else 0 for row in samples]
        def features(name):
            arm = arms[name]
            if arm['correct']:
                observed = ['npu-event', 'source-bound', 'binary-bound', 'score:-log(milliseconds)', 'device:' + str(args.device)]
            else:
                result = arm['receipt']['data'].get('result', {})
                failure = next((row[name] for row in samples if row[name] and not row[name]['correct']), None)
                if failure:
                    result = {**result, 'failure_stage': failure['failure_stage'], 'correctness_info': failure['info']}
                info = arm.get('runtime_error') or result.get('compile_info', result.get('correctness_info', arm['receipt']['data'].get('error', '')))
                from kernelcat_executor.backends.ascend.compiler import diagnostic_lines
                errors = diagnostic_lines(str(info))
                observed = ['failure_stage:' + str(result.get('failure_stage', 'unknown'))]
                observed += ['diagnostic:' + line[:380] for line in errors]
            return {'surface': ['AscendC'], 'lowered': None, 'runtime': observed}
        return {'measurement': {'measurement_id': 'native:' + digest(request), 'task_id': request['task_id'],
            'environment_id': 'npu:' + digest(environments), 'protocol_id': evidence['protocol'],
            'keep_digest': digest(request['keep']), 'remove_digest': digest(request['remove']),
            'sample_ids': [r['sample_id'] for r in samples], 'keep_log_scores': scores('keep'), 'remove_log_scores': scores('remove'),
            'keep_correct': bool(arms['keep']['correct']), 'remove_correct': bool(arms['remove']['correct']),
            'independent_confirmation': request['operation'] == 'confirm',
            'evidence_ids': ['evidence-sha256:' + digest(evidence), 'artifact:' + str(attempt)],
            'features': features('keep'), 'remove_features': features('remove'),
            'visible_coverage': ['float32', 'length:128', 'seeded-finite-inputs'],
            'holdout_coverage': ['independent-sample-namespace'] if request['operation'] == 'confirm' else None,
            'keep_complexity': complexity(request['keep']), 'remove_complexity': complexity(request['remove'])}}
    finally:
        for arm in arms.values():
            if 'process' in arm:
                process = arm['process']
                try: os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError: pass
                process.wait()
        for handle in handles:handle.close()


def main():
    if len(sys.argv) == 4 and sys.argv[1] == '--worker':
        worker(Path(sys.argv[2]), int(sys.argv[3]));return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--reference', type=Path, required=True)
    parser.add_argument('--soc', required=True)
    parser.add_argument('--device', type=int, required=True)
    args = parser.parse_args();args.state=args.state.resolve();args.reference=args.reference.resolve()
    action = json.load(sys.stdin);request = action['arguments']
    attempt = args.state / 'attempts' / digest({'id':action['id'], 'request':request})
    attempt.mkdir(parents=True, exist_ok=False)
    (attempt / 'action.json').write_text(json.dumps(action, indent=2))
    if request['operation'] == 'decompose':
        before, after = source(request['baseline']), source(request['keep'])
        units=[] if before==after else [{'unit_id':'program:'+digest(request['keep'])[:16], 'anchor':'SourceFile:solution.py',
            'path':'solution.py', 'before':before, 'after':after, 'dependencies':['solution.py'], 'ku_ids':[]}]
        result={'decomposition':{'units':units,'candidate_complexity':complexity(request['keep']), 'baseline_complexity':complexity(request['baseline'])}}
    else:
        result=measure(request,args,attempt)
    response={'status':'completed','data':{'rsi':result}}
    (attempt / 'response.json').write_text(json.dumps(response, indent=2))
    print(json.dumps(response))


if __name__=='__main__':main()
