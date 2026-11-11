"""Freeze opt-in business tools without importing operator code or calling devices."""
import argparse
import importlib.util
import json
from pathlib import Path


def prepare(profile, output, python):
    result = list(profile.get('business_tools', []))
    for name in ('operator_infra', 'operator_pipeline'):
        if not profile.get(name): continue
        path = Path(__file__).parent / name / 'prepare.py'
        spec = importlib.util.spec_from_file_location(name + '_prepare', path)
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        result.extend(module.prepare(profile[name], Path(output) / name, python))
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--python', required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(json.loads(args.config.read_text()), args.output, args.python)))
