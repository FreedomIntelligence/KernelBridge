"""Freeze task + trusted RSI configuration for the same Pi SDK with a LoopX goal."""
import argparse
import json
from pathlib import Path
from urllib.parse import unquote, urlparse


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('bundle', 'task', 'config', 'contract', 'baseline', 'python', 'node',
                 'expected-python-modules', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    for name, value in vars(args).items():
        if not value.is_absolute():
            parser.error('--' + name.replace('_', '-') + ' must be absolute')
    bundle = args.bundle.resolve()
    output = args.output.resolve()
    if output == bundle or output.is_relative_to(bundle) or bundle.is_relative_to(output):
        parser.error('output must be disjoint from the source bundle')
    config = json.loads(args.config.read_text())
    task = json.loads(args.task.read_text())
    contract = json.loads(args.contract.read_text())
    binding = json.loads(args.expected_python_modules.read_text())
    if not config.get('rsi') or config.get('evaluator'):
        parser.error('goal mode requires a pinned RSI adapter, not the ordinary evaluator')
    if not 1 <= config['rsi']['timeout'] <= 3600:
        parser.error('RSI timeout must be 1..3600 seconds')
    roots = {str(Path(unquote(urlparse(root).path)).parent)
             for module in binding['modules'] for root in module['allowed_package_roots']}
    if len(roots) != 1:
        parser.error('example requires one frozen Python deployment root')
    config.update(workspace=str(output / 'workspace'), state_dir=str(output / 'state'),
                  python_modules=binding, writable_paths=['solution.py'])
    task['seed_files']['solution.py'] = args.baseline.read_text()
    command = {'argv': [str(args.node), str(bundle / 'source/apps/agent/src/main.ts'), 'run',
        '--python', str(args.python), '--expected-python-modules', str(args.expected_python_modules),
        '--config', str(output / 'config.json'), '--task', str(output / 'task.json'),
        '--timeout-ms', '7200000'], 'python_path': roots.pop(),
        'api_key_env': config['providers']['primary']['api_key_env']}
    output.mkdir()  # Never overwrite or resume an existing invocation implicitly.
    for name, value in [('config.json', config), ('task.json', task),
                        ('task-contract.json', contract), ('command.json', command)]:
        with (output / name).open('x') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
    print(output)


if __name__ == '__main__':
    main()
