"""Create an editable max/min campaign and an opt-in pipeline profile; no execution."""
import argparse
import json
from pathlib import Path
import shutil
import sys
from ascend_sources import candidate


def prepare(output, python):
    output = Path(output).resolve(); output.mkdir(parents=True, exist_ok=False)
    here = Path(__file__).resolve().parent
    for name in ('native.cpp', 'reference.py', 'torch_api.py'): shutil.copyfile(here / name, output / name)
    cases = []
    for reduce in ('max', 'min'):
        for label, rows, features, bins in [('empty', 0, 4, 3), ('singleton', 1, 1, 1),
            ('sparse', 5, 7, 9), ('duplicate', 31, 8, 5), ('collision', 48, 17, 8), ('wide', 129, 65, 33)]:
            cases.append({'id': reduce + '-' + label, 'reduce': reduce, 'rows': rows, 'features': features, 'bins': bins,
                          'seed': 127, 'duplicates': label == 'duplicate', 'all_one': label == 'collision', 'family': label})
    contract = {'task': 'FP32 feature-index scatter max/min values, dimension 0; no input mutation',
        'oracle': {'kind': 'independent', 'description': 'Grouped Python max/min; independent from native scatter',
                   'files': {'reference.py': str(output / 'reference.py')}},
        'suite': cases, 'measurement': {'scope': 'local_cpu', 'timer': 'perf_counter_ns', 'unit': 'us', 'warmup': 3, 'samples': 9},
        'device_acceptance': 'pending', 'finite_inputs': True, 'arg_indices': False}
    profile = {'read_roots': [str(output)], 'drivers': {
        name: {'kind': kind, 'argv': [str(Path(python).resolve()), '-B', str(here / 'driver.py'), mode],
               'files': [str(here / 'driver.py')], 'timeout': 120}
        for name, kind, mode in [('check', 'check', 'check'), ('measure', 'measure', 'measure')]}}
    diagnostic_files = [here / 'diagnostic.py', here / 'driver.py', *sorted(p for p in (here.parent / 'cann/tools').rglob('*') if p.is_file() and '__pycache__' not in p.parts)]
    for name in ('ir', 'profiler'):
        profile['drivers'][name] = {'kind': 'diagnostic', 'argv': [str(Path(python).resolve()), '-B', str(here / 'diagnostic.py'), name],
            'files': list(map(str, diagnostic_files)), 'timeout': 180}
    for reduce in ('max', 'min'):
        folder = output / ('ascend-' + reduce); folder.mkdir()
        (folder / 'solution.py').write_text(candidate(reduce, 33))
        (folder / 'contract.json').write_text(json.dumps({'kind': 'scatter_' + reduce, 'dtype': 'float32',
            'index_dtype': 'int64', 'shape': [129, 65], 'index_shape': [129], 'dim': 0, 'dim_size': 33,
            'soc': 'ai_core-Ascend910_9382', 'values_only': True, 'finite_inputs': True,
            'evidence': 'candidate_source_only; device compilation and correctness pending'}, indent=2) + '\n')
    (output / 'contract.json').write_text(json.dumps(contract, indent=2) + '\n')
    (output / 'pipeline-profile.json').write_text(json.dumps({'operator_pipeline': profile}, indent=2) + '\n')
    return {'output': str(output), 'contract': str(output / 'contract.json'), 'profile': str(output / 'pipeline-profile.json')}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('--output', required=True); parser.add_argument('--python', default=sys.executable)
    args = parser.parse_args(); print(json.dumps(prepare(args.output, args.python)))
