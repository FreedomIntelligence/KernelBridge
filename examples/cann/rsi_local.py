"""Run a pinned NPU adapter with its explicitly configured Python and SDK environment."""
import argparse
import json
import os
from pathlib import Path
import runpy
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('config', type=Path)
    parser.add_argument('--runtime', action='store_true')
    parser.add_argument('--check-runtime', action='store_true')
    parser.add_argument('--diagnostics', action='store_true')
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    if not args.runtime:
        python = Path(config['python'])
        if str(python.resolve()) != config['python_real']:
            raise ValueError('Configured NPU interpreter target changed')
        environment = dict(config['environment'])
        environment['PYTHONDONTWRITEBYTECODE'] = '1'
        snapshot = os.environ.get('KERNELCAT_RSI_SOURCE_MANIFEST')
        if snapshot:
            environment['KERNELCAT_RSI_SOURCE_MANIFEST'] = snapshot
        command = [str(python), '-B', str(Path(__file__).resolve()), str(args.config), '--runtime']
        if args.check_runtime:
            command.append('--check-runtime')
        if args.diagnostics:
            command.append('--diagnostics')
        os.execve(str(python), command, environment)
    if args.check_runtime:
        import torch
        import torch_npu
        import yaml
        import pydantic
        from kernelcat_executor.evaluation.evaluate import run_job
        torch_npu.npu.set_device(config['device'])
        print(json.dumps({'python': sys.executable, 'prefix': sys.prefix,
                          'device': torch_npu.npu.get_device_name(config['device']),
                          'torch': torch.__version__, 'torch_npu': torch_npu.__version__,
                          'yaml': yaml.__version__, 'pydantic': pydantic.__version__,
                          'evaluator': run_job.__module__}))
        return
    bundle = Path(__file__).resolve().parents[2]
    if args.diagnostics:
        sys.path.insert(0, str(bundle / 'examples/cann/tools'))
        from diagnostics import main as diagnostics
        print(json.dumps(diagnostics(config)))
        return
    sys.path.insert(0, str(bundle / 'validation'))
    name = 'cannbench_sigmoid_adapter.py' if config['kind'] == 'sigmoid' else 'native_rsi_adapter.py'
    sys.argv = [str(bundle / 'validation' / name), '--state', config['state'],
                '--soc', config['soc'], '--device', str(config['device'])]
    if config['kind'] == 'sigmoid':
        sys.argv += ['--suite', config['suite']]
    else:
        sys.argv += ['--reference', config['reference'], '--operator', config['operator'],
                     '--atol', str(config['atol']), '--rtol', str(config['rtol'])]
    runpy.run_path(str(bundle / 'validation' / name), run_name='__main__')


if __name__ == '__main__':
    main()
