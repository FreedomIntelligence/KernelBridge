"""Exercise ordinary codegen via a real PTY, actual files and original receipts."""
import argparse
import fcntl
import hashlib
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import pty
import select
import signal
import struct
import subprocess
import shutil
import sys
import termios
import threading
import time

import pyte
from codegen_fixture import CodegenProvider
from product_scenario import ROOT, cleanup_fixture_hosts
from pty_scenario import Client, stop_child
from timing import SCALE, StepTimeout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--launcher', type=Path)
    parser.add_argument('--deployment', type=Path)
    parser.add_argument('--turns', type=int, default=35)
    parser.add_argument('--timeout-seconds', type=float, default=90, help='per-condition deadline on a loaded workstation')
    args = parser.parse_args()
    if not args.launcher and not args.deployment: parser.error('--launcher or --deployment is required')
    output = args.output.resolve(); output.mkdir(parents=True, exist_ok=False)
    project = output / 'project'; project.mkdir()
    (project / 'solution.py').write_text('x = 1\n')
    # Large/binary/symlink/dependency content must not trigger experiment snapshots.
    (project / 'asset.bin').write_bytes(b'\x00' * 1024 * 1024)
    (project / 'link.py').symlink_to('solution.py')
    (project / 'node_modules').mkdir()
    (project / 'node_modules/large.bin').write_bytes(b'\x00' * 17 * 1024 * 1024)
    (project / 'AGENTS.md').write_text('Preserve the constraint CODEGEN_PROJECT_INSTRUCTION.\n')
    provider = ThreadingHTTPServer(('127.0.0.1', 0), CodegenProvider)
    threading.Thread(target=provider.serve_forever, daemon=True).start()
    config = output / 'model.json'
    config.write_text(json.dumps({'providers': {'primary': {'model': 'local-codegen',
        'base_url': f'http://127.0.0.1:{provider.server_port}/v1', 'api_key_env': 'KERNELBRIDGE_TEST_KEY',
        # Two automatic retries keep FAIL_ALWAYS short (about 1.5-3 s of backoff).
        'max_retries': 2}}}))
    registry = Path('/tmp').resolve() / ('kb-code-' + hashlib.sha256(str(output).encode()).hexdigest()[:10])
    home = output / 'home'
    env = {**os.environ, 'TERM': 'xterm-256color', 'PI_OFFLINE': '1', 'KERNELBRIDGE_TEST_KEY': 'fixture-not-secret',
           'KERNELBRIDGE_SESSIONS': str(registry), 'KERNELBRIDGE_HOME': str(home), 'KERNELBRIDGE_CONFIG': str(config)}
    launcher = [str(args.launcher.resolve())] if args.launcher else [shutil.which('node'), str(ROOT / 'source/apps/agent/src/main.ts'),
        '--python', sys.executable, '--expected-python-modules', str(args.deployment.resolve() / 'expected.json')]
    if args.deployment: env['PYTHONPATH'] = str(args.deployment.resolve() / 'unpacked')
    checks = []; master = slave = child = client = None
    transcript = bytearray(); screen = pyte.Screen(110, 35); stream = pyte.ByteStream(screen)
    def launch(arguments=()):
        nonlocal master, slave, child, screen, stream
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 35, 110, 0, 0))
        screen = pyte.Screen(110, 35); stream = pyte.ByteStream(screen)
        child = subprocess.Popen([*launcher, *arguments], cwd=project, env=env,
            stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
    def text(): return '\n'.join(screen.display)
    def pump():
        if select.select([master], [], [], .1)[0]:
            try: data = os.read(master, 65536)
            except OSError: return
            transcript.extend(data); stream.feed(data)
            if b'\x1b[6n' in data: os.write(master, b'\x1b[1;1R')
    def until(predicate, timeout=None):
        timeout = timeout or args.timeout_seconds
        deadline = time.monotonic() + timeout * SCALE
        while time.monotonic() < deadline:
            pump()
            if predicate(): return
            if child.poll() is not None: raise AssertionError(f'CLI exited {child.returncode}\n{text()}')
        raise StepTimeout(f'TIMEOUT after {timeout * SCALE:.0f}s (base {timeout}s x scale {SCALE})\n{text()}')
    def idle(duration=1):
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline: pump()
    def state(): return client.call('status')
    def recorded(message):
        return any(item['text'] == message and item['status'] in {'completed', 'failed', 'interrupted'} for item in state()['inputs'].values())
    def send(message, expected='completed'):
        previous = set(state()['inputs'])
        os.write(master, message.encode() + b'\r')
        until(lambda: any(key not in previous and item['text'] == message and item['status'] in {'completed','failed','interrupted'}
                          for key,item in state()['inputs'].items()))
        idle(.2)
        receipt = next(item for key,item in state()['inputs'].items() if key not in previous and item['text'] == message)
        assert receipt['status'] == expected, receipt
    def capture(name):
        idle(.3); (output / (name + '.txt')).write_text(text())
    def quit():
        os.write(master, b'/quit\r')
        until(lambda: child.poll() is not None)
        assert child.returncode == 0
        assert b'\x1b[?2004l' in transcript
        os.close(master); os.close(slave)
    try:
        launch()
        until(lambda: '等待输入' in text())
        idle()
        application = next(home.glob('runs/*/application.json'))
        directory = Path(json.loads(application.read_text())['host_directory'])
        client = Client(directory); client.socket.settimeout(args.timeout_seconds)
        assert not CodegenProvider.requests and not state()['inputs'] and state()['budget']['spent'] == 0
        assert 'KernelBridge' in text() and 'ctrl+o' in text() and 'Pi can explain' not in text(), text()
        assert '预算' not in text() and 'Trial' not in text() and 'workspace history' not in text()
        capture('idle'); checks.append('native_pi_idle_zero_execution')
        send('/new')
        send('!printf NATIVE_SHELL > idle-shell.txt')
        assert not CodegenProvider.requests and (project / 'idle-shell.txt').read_text() == 'NATIVE_SHELL'
        checks.append('native_commands_before_first_prompt')
        os.write(master, '/help\r'.encode()); until(lambda: '/new' in text())
        first = 'Read solution.py, change x = 1 to x = 2, then verify it with bash. 中文'
        os.write(master, first.encode() + b'\r')
        until(lambda: recorded(first))
        until(lambda: 'CODEGEN_DONE_3' in text())
        assert (project / 'solution.py').read_text() == 'x = 2\n'
        assert (project / 'shell-check.txt').read_text() == 'checked\n'
        current = state()
        assert current['cwd'] == str(project) and current['goal'] is None and current['trials'] == []
        assert current['budget']['limit'] is None and current['max_trial_turns'] is None
        assert not (project / '.git').exists()
        assert 'CODEGEN_PROJECT_INSTRUCTION' in json.dumps(CodegenProvider.requests[0])
        active_tools = {t['function']['name'] for t in CodegenProvider.requests[0]['tools']}
        assert not {'finish_goal', 'update_goal', 'evaluate_candidate', 'context_transition'} & active_tools
        assert 'delegate' in active_tools and not state()['subagents']
        count = len(CodegenProvider.requests); idle(); assert count == len(CodegenProvider.requests)
        capture('completed'); checks.append('read_edit_bash_project_and_return_idle')
        for turn in range(args.turns): send(f'普通回合 {turn}，只回复完成。')
        current = state()
        assert current['model_turns'] == args.turns + 4 and not current['trials']
        checks.append('no_default_step_limit')
        os.write(master, b'/subagents-load-profile\r'); until(lambda: '不可用的命令' in text()); os.write(master, b'\x03')
        capture('unsupported-command'); checks.append('command_gate')
        send('/compact')
        send('After compaction, preserve 中文.')
        send('/new')
        send('New conversation, preserve working files.')
        assert (project / 'solution.py').read_text() == 'x = 2\n' and not state()['trials']
        assert first not in json.dumps(CodegenProvider.requests[-1], ensure_ascii=False)
        checks.append('compact_new_preserve_files')
        count = len(CodegenProvider.requests)
        os.write(master, b'SLOW_INPUT\r')
        until(lambda: len(CodegenProvider.requests) > count)
        os.write(master, b'\x1b')
        until(lambda: recorded('SLOW_INPUT'))
        assert next(item for item in state()['inputs'].values() if item['text'] == 'SLOW_INPUT')['status'] == 'interrupted'
        send('Continue after the explicit interruption.')
        assert not state()['pending_effect_ids']
        checks.append('interrupt_then_continue')
        send('/goal 完成本地 codegen 验收')
        until(lambda: state()['goal']['status'] == 'completed')
        assert state()['goal']['completion_kind'] == 'codegen' and not state()['evidence']
        goal_trials = len(state()['trials'])
        send('/clear')
        assert state()['goal']['status'] == 'completed' and len(state()['trials']) == goal_trials
        send('Goal 结束后继续普通对话。')
        assert state()['goal']['status'] == 'cleared'
        checks.append('goal_opt_in_completion_and_return')
        # A transient 503 is retried on the same route as a new admitted attempt.
        before = len(CodegenProvider.requests)
        send('FAIL_ONCE')
        attempts = [item for item in CodegenProvider.requests[before:] if 'FAIL_ONCE' in json.dumps(item, ensure_ascii=False)]
        assert len(attempts) == 2, len(attempts)
        checks.append('transient_provider_failure_retried')
        # Persistent failure: two retries, then a visible failure instead of a silent stop.
        before = len(CodegenProvider.requests)
        send('FAIL_ALWAYS', 'failed')
        attempts = [item for item in CodegenProvider.requests[before:] if 'FAIL_ALWAYS' in json.dumps(item, ensure_ascii=False)]
        assert len(attempts) == 3, len(attempts)
        until(lambda: '模型调用失败' in text() and 'HTTP 503' in text() and '可以直接重新发送' in text())
        failed = next(item for item in state()['inputs'].values() if item['text'] == 'FAIL_ALWAYS')
        assert failed.get('reason') == 'providers_failed', failed
        capture('provider-failure')
        send('Provider recovered; answer this new request.')
        assert not state()['pending_effect_ids']
        checks.append('provider_failure_visible_next_input')
        original = state()['application_session_id']; count = len(CodegenProvider.requests)
        quit(); client.close(); client = None
        launch(['--continue'])
        until(lambda: 'KernelBridge' in text() and 'local-codegen' in text())
        client = Client(directory); client.socket.settimeout(args.timeout_seconds); idle()
        assert state()['application_session_id'] == original and len(CodegenProvider.requests) == count
        send('Resume keeps this project and the conversation.')
        capture('resumed'); checks.append('resume_no_replay')
        quit()
        (output / 'scenario.json').write_text(json.dumps({'status': 'passed', 'checks': checks,
            'model_requests': len(CodegenProvider.requests), 'state': state()}, indent=2, ensure_ascii=False))
        print(json.dumps({'status': 'passed', 'checks': checks, 'requests': len(CodegenProvider.requests)}), flush=True)
    finally:
        (output / 'transcript.txt').write_bytes(transcript)
        (output / 'model-requests.json').write_text(json.dumps(CodegenProvider.requests, indent=2, ensure_ascii=False))
        try:
            if child is not None: stop_child(child)
        finally:
            if client: client.close()
            for run in home.glob('runs/*'): cleanup_fixture_hosts(run)
            provider.shutdown()


if __name__ == '__main__': main()
