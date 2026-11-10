"""Compile editable CANN inputs without importing candidate code or starting effects."""
from pathlib import Path
import argparse
import ast
import hashlib
import json
import math

from make_tasks import build_task, FIELDS


def read_local(root, name):
    if not isinstance(name, str) or Path(name).name != name or name in ('', '.', '..'):
        raise ValueError('task references must be simple filenames inside the task directory')
    path = root / name
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024 * 1024:
        raise ValueError('task input must be a bounded regular file: ' + name)
    return path.read_text(encoding='utf8')


def validate_case(case):
    common = {'kind', 'task_id', 'native_operator', 'soc', 'device', 'dtype', 'seeds',
              'distribution', 'atol', 'rtol'}
    extra = {'shape'} if case.get('kind') == 'vector_add' else {'m', 'n', 'k', 'accumulator_dtype'}
    if set(case) != common | extra:
        raise ValueError('case contains missing or unsupported fields')
    expected = {'vector_add': ('vector_add', 'float32'), 'matmul': ('matmul', 'float16')}
    if case['kind'] not in expected or (case['native_operator'], case['dtype']) != expected[case['kind']]:
        raise ValueError('these baselines support FP32 vector_add and FP16 matmul only')
    if case['kind'] == 'matmul' and case['accumulator_dtype'] != 'float32':
        raise ValueError('Cube baseline requires FP32 accumulation')
    if type(case['device']) is not int or case['device'] < 0:
        raise ValueError('device must be a nonnegative integer')
    for name in ('atol', 'rtol'):
        if type(case[name]) not in (int, float) or not math.isfinite(case[name]) or case[name] < 0:
            raise ValueError(name + ' must be finite and nonnegative')
    if len(set(case['seeds'])) != 5:
        raise ValueError('five distinct correctness seeds are required')
    if not isinstance(case['task_id'], str) or not case['task_id'].strip():
        raise ValueError('task_id is required')
    if not isinstance(case['soc'], str) or not case['soc'].startswith('ai_core-Ascend'):
        raise ValueError('an explicit Ascend target is required')


def load_bundle(path, mode='generate'):
    path = Path(path).absolute()
    author_text = read_local(path.parent, path.name)
    author = json.loads(author_text)
    if set(author) != {'schema_version', 'goal', 'case_file', 'baseline_file'}:
        raise ValueError('expected the CANN authoring task schema; use task.compiled.json with runTask directly')
    if author['schema_version'] != 'kernelbridge.cann-task/v1':
        raise ValueError('unsupported CANN task schema')
    if not isinstance(author['goal'], str) or not 1 <= len(author['goal'].strip()) <= 8000:
        raise ValueError('goal must contain 1..8000 characters')
    case_text = read_local(path.parent, author['case_file'])
    baseline = read_local(path.parent, author['baseline_file'])
    case = json.loads(case_text)
    validate_case(case)
    tree = ast.parse(baseline)
    names = {target.id for statement in tree.body if isinstance(statement, ast.Assign)
             for target in statement.targets if isinstance(target, ast.Name)}
    if not set(FIELDS) <= names:
        raise ValueError('baseline must define the six native source fields')
    task, reference, request = build_task(case, baseline, author['goal'], mode)
    inputs = {path.name: author_text, author['case_file']: case_text, author['baseline_file']: baseline}
    manifest = {'schema_version': 'kernelbridge.cann-inputs/v1', 'mode': mode,
                'source_directory': str(path.parent), 'inputs': {
                    name: {'sha256': hashlib.sha256(text.encode()).hexdigest(), 'bytes': len(text.encode())}
                    for name, text in inputs.items()}}
    return task, reference, request, manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--task', type=Path, required=True)
    parser.add_argument('--mode', choices=['generate', 'optimize'], default='generate')
    args = parser.parse_args()
    task, reference, request, manifest = load_bundle(args.task, args.mode)
    print(json.dumps({'task': task, 'reference': reference, 'request': request, 'manifest': manifest}, ensure_ascii=False))


if __name__ == '__main__':
    main()
