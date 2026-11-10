"""Trusted local-NPU evaluator. Run on a provisioned, isolated CANN device host."""
import hashlib
import json
import os
from pathlib import Path
import sys


def main():
    template = json.loads(Path(sys.argv[1]).read_text())
    runtime = template.pop('_runtime', None)
    if runtime and sys.argv[2:] != ['--runtime']:
        python = Path(runtime['python'])
        if str(python.resolve()) != runtime['python_real']:
            raise ValueError('Configured NPU interpreter target changed')
        environment = {**runtime['environment'], 'PYTHONDONTWRITEBYTECODE': '1'}
        os.execve(str(python), [str(python), '-B', str(Path(__file__).resolve()),
                               sys.argv[1], '--runtime'], environment)
    from kernelcat_executor.evaluation.evaluate import run_job
    from kernelcat_executor.evaluation.protocols.six_source.config import Request

    action = json.load(sys.stdin)
    root = Path.cwd().resolve()
    baseline = template.pop('_baseline', None)
    name = action['arguments'].get('candidate', 'solution.py')
    candidate = root / name
    if name != 'solution.py' or candidate.is_symlink() or candidate.resolve() != candidate:
        raise ValueError('only admitted solution.py is evaluated')
    if action['tool'] == 'baseline_check':
        if baseline is None:
            raise ValueError('No frozen baseline configured')
        candidate = Path(baseline)
    if action['tool'] in ('baseline_check', 'operator_check'):
        template['fast'] = True
    job = Path(template.pop('jobs')) / hashlib.sha256(action['id'].encode()).hexdigest()
    # run_job reserves the new job directory; an existing identity is never rerun.
    request = Request.from_dict({**template, 'candidate': str(candidate), 'workspace': str(job)})
    print(json.dumps(run_job(request)))


if __name__ == '__main__':
    main()
