"""Self-contained local TUI acceptance; never uses a real model or NPU profile."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True, help='new canonical directory for original logs and receipts')
    parser.add_argument('--jobs', type=int, choices=range(1, 5), default=1, help='parallel checks; default 1 limits memory pressure')
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    node = shutil.which('node')
    if node is None: raise RuntimeError('Node 22.19+ must be on PATH')
    environment = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'}
    checks = []

    def execute(name, command, timeout=300):
        started = time.monotonic()
        with (output / (name + '.log')).open('wb') as log:
            result = subprocess.run(command, cwd=ROOT, env=environment, stdout=log, stderr=subprocess.STDOUT, timeout=timeout)
        record = {'check': name, 'exit_code': result.returncode, 'seconds': round(time.monotonic() - started, 2)}
        print(json.dumps(record), flush=True)
        return record

    deployment = output / 'deployment'
    build = execute('deployment', [sys.executable, '-B', str(ROOT / 'source/scripts/python_deployment.py'),
        '--repository', str(ROOT / 'source'), '--output', str(deployment)])
    checks.append(build)
    if build['exit_code']: raise RuntimeError('frozen deployment failed; inspect deployment.log')
    commands = {
        'authority': [sys.executable, '-B', '-m', 'unittest', 'discover', '-s', 'validation/tui', '-p', 'test_*.py', '-v'],
        'compatibility': [node, '--test', 'validation/codegen.mjs', 'validation/tui/compatibility.mjs', 'validation/tui/interaction.mjs', 'validation/pi-compatibility.mjs',
                          'validation/workspace-tools.mjs', 'validation/environment.mjs'],
        'runtime': [sys.executable, '-B', '-m', 'unittest', 'discover', '-s', 'validation', '-p', 'test_runtime.py', '-v'],
        'types': [node, 'source/apps/agent/node_modules/typescript/bin/tsc', '--noEmit', '-p', 'source/apps/agent/tsconfig.json'],
    }
    for name in ['sdk', 'long', 'recovery', 'steering', 'lifecycle', 'transport', 'admission', 'fork']:
        commands[name] = [sys.executable, '-B', 'validation/tui/product_scenario.py', '--deployment', str(deployment),
                          '--output', str(output / name), '--scenario', name]
    commands['pty'] = [sys.executable, '-B', 'validation/tui/pty_scenario.py', '--deployment', str(deployment), '--output', str(output / 'pty')]
    for name, script in [('codegen', 'codegen_scenario.py'), ('codegen-edges', 'codegen_edges.py')]:
        commands[name] = [sys.executable, '-B', 'validation/tui/' + script, '--deployment', str(deployment), '--output', str(output / name)]
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        checks.extend(pool.map(lambda item: execute(*item, timeout=1200 if item[0] == 'codegen' else 300), commands.items()))
    evidence = {}
    for path in output.rglob('*'):
        if path.is_file() and (path.name in {'scenario.json', 'events.jsonl', 'pty-transcript.txt', 'model-requests.json'} or path.parent == output and path.suffix == '.log'):
            evidence[path.relative_to(output).as_posix()] = {'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'bytes': path.stat().st_size}
    summary = {'schema_version': 'kernelbridge.local-tui-acceptance/v1', 'status': 'passed' if all(check['exit_code'] == 0 for check in checks) else 'failed',
        'platform': sys.platform, 'scope': 'local deterministic provider and evaluator; no NPU qualification',
        'checks': checks, 'evidence': evidence, 'device_qualification': 'pending'}
    (output / 'acceptance.json').write_text(json.dumps(summary, indent=2) + '\n')
    return 0 if summary['status'] == 'passed' else 1


if __name__ == '__main__': raise SystemExit(main())
