"""Opt-in real-device compile/load/timing and failure checks in a provisioned NPU runner."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'source/packages/executor/src'))
from kernelcat_executor.evaluation.evaluate import run_job
from kernelcat_executor.evaluation.protocols.six_source.config import Request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--device', type=int, required=True)
    parser.add_argument('--soc', required=True)
    parser.add_argument('--ack-isolated-runner', action='store_true')
    args = parser.parse_args()
    if not args.ack_isolated_runner:
        parser.error('provision native execution isolation and pass --ack-isolated-runner')
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    fixture = Path(__file__).parent / 'fixtures/ascend_add.py'
    reference = Path(__file__).parent / 'fixtures/ascend_add_reference.py'
    results = {}
    checks = {}
    for name, suffix, timeout in (
        ('correct', '', 600),
        ('compile-error', "\nkernel_src += '\\n#error acceptance_compile_marker\\n'\n", 600),
        ('load-error', "\nmodel_src = \"raise RuntimeError('acceptance_load_marker')\"\n", 600),
        ('timeout', '', 1),
    ):
        candidate = output / (name + '.py')
        candidate.write_text(fixture.read_text() + suffix)
        request = Request(candidate, reference.resolve(), output / name, 'audit_add', args.soc, args.device, name != 'correct', timeout)
        result = run_job(request)
        results[name] = result
        native = result['data'].get('result', {})
        if name == 'correct':
            checks[name] = result['status'] == 'completed' and len(native.get('performance', {}).get('samples_ms', [])) == 100
            checks['source-bound'] = result['data']['candidate_sha256'] == hashlib.sha256(candidate.read_bytes()).hexdigest()
            files = native['build']['files']
            checks['binary-bound'] = bool(files) and all(hashlib.sha256((request.workspace / 'build' / f['path']).read_bytes()).hexdigest() == f['sha256'] for f in files)
            before = (request.workspace / 'result.json').read_bytes()
            try:
                run_job(request)
                checks['no-repeat-job'] = False
            except FileExistsError:
                checks['no-repeat-job'] = before == (request.workspace / 'result.json').read_bytes()
        elif name == 'timeout':
            checks[name] = result['status'] == 'unknown' and result['data'].get('error') == 'timeout' and not result['data']['promoted']
        else:
            checks[name] = result['status'] == 'failed' and native.get('failure_stage') == name.replace('-', '_')
        (output / 'progress.json').write_text(json.dumps({'checks': checks, 'results': results}, indent=2))
        print(json.dumps({'case': name, 'check': checks[name], 'artifact': str(request.workspace)}), flush=True)
    (output / 'summary.json').write_text(json.dumps({'checks': checks, 'results': results}, indent=2))
    return 0 if all(checks.values()) else 1


if __name__ == '__main__':
    raise SystemExit(main())
