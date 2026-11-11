"""Open the real TUI with a local scripted model and evaluator, without credentials."""
import argparse
import fcntl
import hashlib
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import threading
from uuid import uuid4

from product_scenario import ROOT, Provider, cleanup_fixture_hosts, prepare


def main():
    if '--experiment' not in sys.argv:
        from codegen_demo import main as codegen_main
        return codegen_main()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--experiment', action='store_true', help='explicit isolated evaluator demo')
    parser.add_argument('--launcher', type=Path, required=True, help='installed kernelagent executable')
    parser.add_argument('--output', type=Path, help='new demo directory, or the original directory with --resume')
    parser.add_argument('--resume', action='store_true', help='restart the local fixture and attach the retained session')
    args = parser.parse_args()
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        parser.error('run this demo in an interactive terminal')
    if args.resume and not args.output:
        parser.error('--resume requires the original --output directory')
    launcher = args.launcher.resolve()
    deployment = launcher.parent / 'runtime'
    if not launcher.is_file() or not (deployment / 'expected.json').is_file():
        parser.error('--launcher must select an installed bundle')
    output = (args.output or ROOT / '.validation' / ('manual-tui-' + uuid4().hex[:12])).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with (output.parent / (output.name + '.demo.lock')).open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.error('this demo is already open in another terminal')
        if args.resume:
            descriptor = json.loads((output / 'demo.json').read_text())
            if descriptor['launcher'] != str(launcher) or descriptor['python'] != sys.executable:
                parser.error('resume with the same launcher and Python executable')
            options = json.loads((output / 'options.json').read_text())
            identity = json.loads((output / 'application.json').read_text())['application_session_id']
            Provider.requests = json.loads((output / 'model-requests.json').read_text())
            Provider.turns.clear()
            for request in Provider.requests:
                Provider.turns[request['model']] += 1
            port = descriptor['port']
            command = [str(launcher), 'tui', '--attach', identity, '--restart']
        else:
            if output.exists():
                parser.error('choose a new --output directory, or use --resume')
            port = 0
        with ThreadingHTTPServer(('127.0.0.1', port), Provider) as provider:
            if not args.resume:
                options = prepare(output, deployment, f'http://127.0.0.1:{provider.server_port}/v1', name='demo')
                (output / 'demo.json').write_text(json.dumps({'launcher': str(launcher), 'python': sys.executable,
                    'port': provider.server_port}, indent=2) + '\n')
                command = [str(launcher), 'tui', '--task', options['task'], '--config', options['config'],
                    '--output', str(output), '--session-only', '--python', sys.executable,
                    '--expected-python-modules', options['expectedPythonModules']]
            registry = Path('/tmp').resolve() / ('kb-demo-' + hashlib.sha256(str(output).encode()).hexdigest()[:10])
            environment = {**os.environ, 'PI_OFFLINE': '1', 'KERNELBRIDGE_DEMO': '1', 'KERNELBRIDGE_TEST_KEY': 'fixture-key-not-a-real-credential',
                'KERNELBRIDGE_SESSIONS': str(registry), 'TERM': os.environ.get('TERM', 'xterm-256color')}
            thread = threading.Thread(target=provider.serve_forever, daemon=True)
            thread.start()
            print(f'Local scripted demo — no external model or NPU.\nRun directory: {output}', flush=True)
            try:
                return subprocess.run(command, env=environment).returncode
            except KeyboardInterrupt:
                return 130
            finally:
                cleanup_fixture_hosts(output)
                provider.shutdown()
                thread.join(timeout=5)
                (output / 'model-requests.json').write_text(json.dumps(Provider.requests, indent=2) + '\n')
                resume = [sys.executable, '-B', str(Path(__file__).resolve()), '--experiment', '--launcher', str(launcher),
                    '--output', str(output), '--resume']
                print('\nDemo stopped; the session and budget are retained. Resume with:\n' + shlex.join(resume), flush=True)


if __name__ == '__main__':
    raise SystemExit(main())
