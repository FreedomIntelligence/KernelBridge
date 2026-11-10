"""Trusted local-NPU evaluator. Run on a provisioned, isolated CANN device host."""
import hashlib
import json
from pathlib import Path
import sys
from kernelcat_executor.evaluation.evaluate import run_job
from kernelcat_executor.evaluation.protocols.six_source.config import Request


def main():
    template = json.loads(Path(sys.argv[1]).read_text())
    action = json.load(sys.stdin)
    root = Path.cwd().resolve()
    name = action['arguments']['candidate']
    candidate = root / name
    if name != 'solution.py' or candidate.is_symlink() or candidate.resolve() != candidate:
        raise ValueError('only admitted solution.py is evaluated')
    job = Path(template.pop('jobs')) / hashlib.sha256(action['id'].encode()).hexdigest()
    # run_job reserves the new job directory; an existing identity is never rerun.
    request = Request.from_dict({**template, 'candidate': str(candidate), 'workspace': str(job)})
    print(json.dumps(run_job(request)))


if __name__ == '__main__':
    main()
