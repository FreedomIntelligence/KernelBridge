"""Self-contained local TUI acceptance; never uses a real model or NPU profile.

Each check ends as passed, flaky, failed or environmental:
- a timeout (StepTimeout, a scenario deadline, or the runner's hard ceiling) is
  retried once in a fresh directory; an assertion failure is never retried;
- a timeout that passes on retry is flaky, and a run with any flaky check is not
  a pass (nextest's --flaky-result fail);
- two timeouts while load average per CPU exceeds HEAVY_LOAD are environmental:
  recorded with their load samples, never counted as passes.
Deadlines inside scenarios are scaled by KB_TIMEOUT_SCALE (validation/tui/timing.py);
the runner sets it from load average per CPU unless it is already set.
"""
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
sys.path.insert(0, str(Path(__file__).resolve().parent))
from timing import default_scale  # noqa: E402

HEAVY_LOAD = 2.0
TIMEOUT_EXCEPTIONS = ('StepTimeout', 'TimeoutExpired', 'TimeoutError', 'socket.timeout', 'timeout')


def first_exception(text):
    """The original exception of a Python failure: chained ones ("During handling ...") come later."""
    lines = text.decode('utf-8', 'replace').splitlines()
    try:
        start = next(index for index, line in enumerate(lines) if line.startswith('Traceback (most recent call last):'))
    except StopIteration:
        return None
    for line in lines[start + 1:]:
        if line and not line.startswith((' ', '\t')):
            return line.split(':', 1)[0].strip()
    return None


def load_per_cpu():
    return round(os.getloadavg()[0] / (os.cpu_count() or 1), 2)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--output', type=Path, required=True, help='new canonical directory for original logs and receipts')
    parser.add_argument('--jobs', type=int, choices=range(1, 5), default=1, help='parallel checks; default 1 (PTY checks contend for CPU)')
    parser.add_argument('--only', nargs='*', help='run only these checks (the deployment is always built)')
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    node, npm = shutil.which('node'), shutil.which('npm')
    if node is None or npm is None: raise RuntimeError('Node 22.19+ and npm must be on PATH')
    scale = os.environ.get('KB_TIMEOUT_SCALE') or str(default_scale())
    environment = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', 'KB_TIMEOUT_SCALE': scale}
    launcher = str(ROOT / 'kernelagent')

    def run_once(name, command, cwd, timeout, attempt):
        log_path = output / (name + ('.retry' if attempt else '') + '.log')
        before, started = load_per_cpu(), time.monotonic()
        try:
            with log_path.open('wb') as log:
                code = subprocess.run(command, cwd=cwd, env=environment, stdout=log, stderr=subprocess.STDOUT,
                                      timeout=timeout * float(scale)).returncode
        except subprocess.TimeoutExpired:
            code = 'timeout'
        text = log_path.read_bytes()
        return {'exit_code': code, 'seconds': round(time.monotonic() - started, 2), 'load_per_cpu': [before, load_per_cpu()],
                'first_exception': first_exception(text),
                'timed_out': code == 'timeout' or (code != 0 and (first_exception(text) or '').split('.')[-1] in {name.split('.')[-1] for name in TIMEOUT_EXCEPTIONS}),
                'slow_steps': text.count(b'SLOW: '), 'log': log_path.name}

    def execute(name, build, cwd=ROOT, timeout=300):
        attempts = [run_once(name, build(name), cwd, timeout, 0)]
        if attempts[0]['exit_code'] == 0:
            outcome = 'passed'
        elif not attempts[0]['timed_out']:
            outcome = 'failed'
        else:
            attempts.append(run_once(name, build(name + '-retry'), cwd, timeout, 1))
            heavy = max(attempts[0]['load_per_cpu'] + attempts[1]['load_per_cpu']) > HEAVY_LOAD
            outcome = ('flaky' if attempts[1]['exit_code'] == 0
                       else 'environmental' if attempts[1]['timed_out'] and heavy else 'failed')
        record = {'check': name, 'outcome': outcome, 'attempts': attempts}
        print(json.dumps(record), flush=True)
        return record

    deployment = output / 'deployment'
    build = execute('deployment', lambda _name: [sys.executable, '-B', str(ROOT / 'source/scripts/python_deployment.py'),
        '--repository', str(ROOT / 'source'), '--output', str(deployment)])
    if build['outcome'] != 'passed': raise RuntimeError('frozen deployment failed; inspect deployment.log')
    if not (ROOT / '.development/ready').exists():
        raise RuntimeError('launcher scenarios need the development runtime: run source/scripts/develop.py first')

    py = [sys.executable, '-B']
    def scenario(script, *extra):
        return lambda name: [*py, 'validation/tui/' + script, '--output', str(output / name), *extra]
    checks = {
        'authority': (lambda _n: [*py, '-m', 'unittest', 'discover', '-s', 'validation/tui', '-p', 'test_*.py', '-v'], ROOT, 300),
        'runtime': (lambda _n: [*py, '-m', 'unittest', 'discover', '-s', 'validation', '-p', 'test_runtime.py', '-v'], ROOT, 300),
        'js': (lambda _n: [npm, 'test'], ROOT / 'source/apps/agent', 600),
    }
    for name in ['sdk', 'long', 'recovery', 'steering', 'lifecycle', 'transport', 'admission', 'fork']:
        checks[name] = (scenario('product_scenario.py', '--deployment', str(deployment), '--scenario', name), ROOT, 300)
    for name, script, timeout in [('pty', 'pty_scenario.py', 300), ('interaction', 'interaction_scenario.py', 300),
                                  ('codegen', 'codegen_scenario.py', 1200), ('codegen-edges', 'codegen_edges.py', 300)]:
        checks[name] = (scenario(script, '--deployment', str(deployment)), ROOT, timeout)
    for name, script in [('advanced', 'advanced_scenario.py'), ('resume', 'resume_default_home.py'), ('trace', 'trace_scenario.py'),
                         ('subagents-parallel', 'subagent_parallel.py'), ('subagents-stop', 'subagent_stop_scenario.py'),
                         ('subagents-panel', 'subagent_panel_scenario.py')]:
        checks[name] = (scenario(script, '--launcher', launcher), ROOT, 600)
    for name, script in [('cancel', 'subagent_cancel_scenario.py'), ('goal-review', 'goal_review_scenario.py'),
                         ('goal-plan', 'goal_plan_scenario.py'), ('pi-plugin-trace', 'pi_plugin_trace_scenario.py'),
                         ('trace-branches', 'trace_branches_render.py')]:
        checks[name] = (scenario(script), ROOT, 600)
    selected = [name for name in checks if not args.only or name in args.only]
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        records = [build, *pool.map(lambda name: execute(name, *checks[name]), selected)]

    evidence = {}
    for path in output.rglob('*'):
        if path.is_file() and (path.name in {'scenario.json', 'events.jsonl', 'pty-transcript.txt', 'transcript.txt', 'model-requests.json'} or path.suffix == '.cast'
                               or path.parent == output and path.suffix == '.log'):
            evidence[path.relative_to(output).as_posix()] = {'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'bytes': path.stat().st_size}
    counts = {outcome: sum(record['outcome'] == outcome for record in records) for outcome in ('passed', 'flaky', 'failed', 'environmental')}
    status = ('passed' if counts['passed'] == len(records) else 'failed' if counts['failed']
              else 'flaky' if counts['flaky'] else 'inconclusive')
    summary = {'schema_version': 'kernelbridge.local-tui-acceptance/v2', 'status': status, 'counts': counts,
        'timeout_scale': float(scale), 'heavy_load_per_cpu': HEAVY_LOAD, 'cpus': os.cpu_count(),
        'platform': sys.platform, 'scope': 'local deterministic provider and evaluator; no NPU qualification',
        'checks': records, 'evidence': evidence, 'device_qualification': 'pending'}
    (output / 'acceptance.json').write_text(json.dumps(summary, indent=2) + '\n')
    print(json.dumps({'status': status, 'counts': counts, 'timeout_scale': float(scale)}), flush=True)
    return 0 if status == 'passed' else 1


if __name__ == '__main__': raise SystemExit(main())
