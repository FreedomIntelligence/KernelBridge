"""Read-only acceptance of a generated CANN task, independent of the model summary."""
from pathlib import Path
import argparse
import hashlib
import json
import math

from kernelcat_executor.contracts.models import Task, digest
from kernelcat_executor.profiles.store import AgentJournal


def verify(output):
    config = json.loads((output / 'config.json').read_text())
    task = Task.model_validate_json((output / 'task.json').read_text())
    result = json.loads((output / 'result.json').read_text())
    workspace = Path(config['workspace']).resolve() / digest(task)[:20]
    path = workspace / 'solution.py'
    if path.is_symlink() or path.resolve() != path or not path.is_file():
        raise ValueError('final candidate must be a regular admitted source file')
    candidate = hashlib.sha256(path.read_bytes()).hexdigest()
    reference = hashlib.sha256(task.seed_files['reference/reference.py'].encode()).hexdigest()
    state = AgentJournal(Path(config['state_dir']) / 'events.jsonl').snapshot()
    actions = {k: v for k, v in state['actions'].items() if v['action']['task'] == task.id}
    pending = [k for k, v in actions.items() if not v.get('terminal')]
    if result.get('status') != 'finished' or pending:
        raise ValueError('Pi must finish normally with no unresolved admitted effects')
    matches = []
    for identity, row in actions.items():
        if row['action']['tool'] != 'evaluate':
            continue
        outcome = row.get('outcome', {})
        data = outcome.get('data', {})
        native = data.get('result') or {}
        performance = native.get('performance') or {}
        samples = performance.get('samples_ms', [])
        if not (row['action']['tool'] == 'evaluate' and outcome.get('status') == 'completed'
                and data.get('candidate_sha256') == candidate and data.get('reference_sha256') == reference
                and native.get('compiled') is True and native.get('correctness') is True
                and native.get('failure_stage') is None
                and native.get('hardware') == task.contract['target']['soc']
                and native.get('correctness_policy') == {k: task.contract['correctness'][k] for k in ('atol', 'rtol')}
                and native.get('build', {}).get('files')
                and performance.get('timer') == 'npu_event'
                and performance.get('num_trials') == task.contract['performance']['samples']
                and len(samples) == task.contract['performance']['samples']
                and all(type(t) in (int, float) and math.isfinite(t) and t > 0 for t in samples)):
            continue
        matches.append(identity)
    if not matches:
        raise ValueError('no original native success receipt matches final source, reference, target and timing contract')
    return {'accepted': True, 'task_id': task.id, 'candidate': str(path), 'candidate_sha256': candidate,
            'reference_sha256': reference, 'evaluation_actions': matches, 'pending_effect_ids': pending,
            'correctness_trials': task.contract['test_data']['correctness_trials'],
            'scope': 'trusted five-seed six-source evaluator; no paired speedup claim'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        result = verify(args.output.resolve())
    except (OSError, ValueError, KeyError) as error:
        print(json.dumps({'accepted': False, 'error': str(error)}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
