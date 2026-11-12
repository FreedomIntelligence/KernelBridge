"""Launch one fresh kernelagent trial and observe its original Journal.

The agent owns the entire optimization loop. This wrapper never writes a
candidate, sends follow-up prompts, retries a device job, or changes a budget.
An observer timeout leaves the original host running with its original handle.
"""
import argparse
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import pty
import select
import struct
import subprocess
import termios
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('operator_trial_audit', ROOT / 'source/scripts/audit_operator_trial.py')
audit_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit_module)


def save(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--task', type=Path, required=True)
    parser.add_argument('--profile', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--timeout', type=float, default=3600)
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists():
        raise ValueError('A fresh trial requires a new output directory; recover old sessions separately')
    output.parent.mkdir(parents=True, exist_ok=True)
    observer = output.parent / (output.name + '-observer')
    observer.mkdir(exist_ok=False)
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 40, 120, 0, 0))
    command = [str(ROOT / 'kernelagent'), 'tui', '--task', str(args.task.resolve(strict=True)),
        '--profile', str(args.profile.resolve(strict=True)), '--output', str(output),
        '--goal-from-task', '--on-detach', 'continue']
    env = dict(os.environ, TERM='xterm-256color', PYTHONDONTWRITEBYTECODE='1',
        KERNELBRIDGE_SESSIONS=str(Path('/tmp').resolve() / ('kb-trial-' + uuid.uuid4().hex[:10])))
    child = subprocess.Popen(command, cwd=ROOT, env=env, stdin=slave, stdout=slave,
        stderr=slave, start_new_session=True)
    save(observer / 'launch.json', {'command': command, 'pid': child.pid, 'observer_only': True,
        'registry': env['KERNELBRIDGE_SESSIONS'], 'started_ns': time.time_ns()})
    started = time.monotonic(); last_poll = 0; previous = None; quit_sent = False
    outcome = {'status': 'observer_timeout', 'original_session_preserved': True}
    try:
        with (observer / 'transcript.bin').open('wb') as transcript:
            while time.monotonic() - started < args.timeout:
                if select.select([master], [], [], .1)[0]:
                    try:
                        chunk = os.read(master, 65536)
                    except OSError:
                        chunk = b''
                    transcript.write(chunk); transcript.flush()
                    if b'\x1b[6n' in chunk:
                        os.write(master, b'\x1b[1;1R')
                if time.monotonic() - last_poll > 10 and (output / 'state/events.jsonl').exists():
                    last_poll = time.monotonic()
                    try:
                        report = audit_module.audit(output)
                    except ValueError as error:
                        if 'incomplete tail' in str(error):
                            continue
                        raise
                    save(observer / 'audit.json', report)
                    brief = {key: report[key] for key in ('model_turns', 'tokens', 'stages_called', 'accepted_e3')}
                    brief['goal_status'] = (report['goal'] or {}).get('status')
                    brief['pending'] = len(report['pending_action_ids'])
                    if brief != previous:
                        print(json.dumps(brief, ensure_ascii=False), flush=True); previous = brief
                    if (brief['goal_status'] in ('completed', 'budget_limit', 'awaiting_user', 'failed')
                            and not brief['pending'] and not quit_sent):
                        outcome = {'status': 'settled', 'goal_status': brief['goal_status'],
                            'accepted_e3': report['accepted_e3'], 'original_session_preserved': True}
                        os.write(master, b'/quit\r'); quit_sent = True
                if child.poll() is not None:
                    outcome['cli_exit_code'] = child.returncode
                    if not quit_sent:
                        outcome['status'] = 'cli_exited_before_observed_settlement'
                    break
    finally:
        outcome['elapsed_seconds'] = time.monotonic() - started
        save(observer / 'outcome.json', outcome)
        os.close(master); os.close(slave)
        # No process-group kill: an observer timeout is not cancellation or
        # permission to relaunch the original admitted device job.
    print(json.dumps(outcome, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
