"""Rendered-screen acceptance for Goal commands, completion, scrolling and footer."""
import argparse
import fcntl
import hashlib
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import pty
import select
import shutil
import signal
import struct
import subprocess
import sys
import termios
import threading
import time

import pyte
from product_scenario import ROOT, Provider, prepare
from pty_scenario import Client, stop_child
from timing import SCALE, StepTimeout

# The isolated-task hint in the empty editor (product-ui.mjs HINTS); it fits 48 columns.
HINT = '输入约束或问题 · /goal 目标内容 持续执行'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--deployment', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--launcher', type=Path)
    parser.add_argument('--timeout-seconds', type=float, default=30, help='per-condition deadline on a loaded workstation')
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists(): raise ValueError('choose a new output directory')
    provider = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
    threading.Thread(target=provider.serve_forever, daemon=True).start()
    options = prepare(output, args.deployment.resolve(), f'http://127.0.0.1:{provider.server_port}/v1', name='ui')
    registry = Path('/tmp').resolve() / ('kb-ui-' + hashlib.sha256(str(output).encode()).hexdigest()[:10])
    # This scenario exercises in-app history scrolling, which only the
    # fullscreen mode provides; the default regular mode uses terminal scrollback.
    env = {**os.environ, 'TERM': 'xterm-256color', 'PI_OFFLINE': '1', 'KERNELBRIDGE_DEMO': '1', 'KERNELBRIDGE_TUI_MODE': 'fullscreen',
        'PYTHONPATH': str(args.deployment.resolve() / 'unpacked'),
        'KERNELBRIDGE_TEST_KEY': 'fixture-key-not-a-real-credential', 'KERNELBRIDGE_SESSIONS': str(registry)}
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 30, 100, 0, 0))
    screen = pyte.Screen(100, 30)
    stream = pyte.ByteStream(screen)
    transcript = bytearray()
    launcher = [str(args.launcher.resolve())] if args.launcher else [shutil.which('node'), str(ROOT / 'source/apps/agent/src/main.ts')]
    child = subprocess.Popen([*launcher, 'tui', '--task', options['task'], '--config', options['config'], '--output', str(output),
        '--python', sys.executable, '--expected-python-modules', options['expectedPythonModules'], '--session-only'],
        stdin=slave, stdout=slave, stderr=slave, start_new_session=True, env=env)
    client, host_pid = None, None
    text = lambda: '\n'.join(screen.display)
    def pump():
        if select.select([master], [], [], .05)[0]:
            try: data = os.read(master, 65536)
            except OSError: return
            transcript.extend(data)
            stream.feed(data)
            if b'\x1b[6n' in data: os.write(master, b'\x1b[1;1R')
    def until(predicate, timeout=None):
        timeout = args.timeout_seconds if timeout is None else timeout
        deadline = time.monotonic() + timeout * SCALE
        while time.monotonic() < deadline:
            pump()
            if predicate(): return
            if child.poll() is not None: raise AssertionError(f'CLI exited {child.returncode}\n{text()}')
        raise StepTimeout(f'TIMEOUT after {timeout * SCALE:.0f}s (base {timeout}s x scale {SCALE})\n{text()}')
    def state(): return client.call('status')
    def recorded(message, status='completed'):
        return any(item['text'] == message and item['status'] == status for item in state()['inputs'].values())
    def send(message):
        os.write(master, message.encode() + b'\r')
        until(lambda: recorded(message))
    def save(name):
        # A terminal frame may span multiple PTY reads; capture after output settles.
        deadline, quiet = time.monotonic() + 3, time.monotonic()
        while time.monotonic() < deadline and time.monotonic() - quiet < .2:
            length = len(transcript)
            pump()
            if len(transcript) != length: quiet = time.monotonic()
        (output / (name + '.txt')).write_text(text())
    try:
        until(lambda: (output / 'application.json').exists())
        directory = Path(json.loads((output / 'application.json').read_text())['host_directory'])
        until(lambda: (directory / 'host.sock').exists())
        client = Client(directory)
        host_pid = state()['host_pid']
        until(lambda: recorded('Read, edit x = 1 to x = 2, and evaluate the local fixture.'))
        until(lambda: 'fixture turn 3' in text() and '预算 15/200' in text())
        assert '回到最新消息' not in text(), text()
        assert b'<inline:' not in transcript
        assert b'Initializing workspace history' not in transcript
        assert b'Invocation stopped by host' not in transcript
        # D5: the empty editor carries a dim hint for this mode.
        until(lambda: HINT in text())
        save('initial-screen')

        # Exercise actual Tab completion, not merely the command parser.
        os.write(master, b'/go')
        until(lambda: '→ goal' in text())
        assert HINT not in text(), 'the first keystroke hides the editor hint'
        os.write(master, b'\t')
        until(lambda: any(line.strip() == '/goal' for line in screen.display))
        os.write(master, b'1\r')
        until(lambda: (state()['goal'] or {}).get('objective') == '1')
        assert state()['task_version'] == 2
        send('/goal pause')
        until(lambda: state()['goal']['status'] == 'paused' and not state()['pending_effect_ids'])
        send('/goal')
        until(lambda: '目标：1' in text() and '已暂停' in text())
        save('goal-screen')
        send('/goal resume')
        send('/goal pause')
        until(lambda: state()['goal']['status'] == 'paused' and not state()['pending_effect_ids'])
        send('/goal clear')
        assert state()['goal']['status'] == 'cleared'

        os.write(master, b'\x1b[1;5H')
        until(lambda: '回到最新消息' in text())
        save('history-screen')
        count = state()['model_turns']
        os.write(master, b'/subagents-load-profile\r')
        until(lambda: recorded('/subagents-load-profile', 'failed'))
        until(lambda: '不可用的命令' in text())
        assert state()['model_turns'] == count
        save('rejected-command-screen')
        os.write(master, b'\x15/latest\r')
        until(lambda: recorded('/latest'))
        until(lambda: '回到最新消息' not in text())
        os.write(master, b'\x1b[1;5H')
        until(lambda: '回到最新消息' in text())
        send('从历史位置提交中文输入')
        until(lambda: '从历史位置提交中文输入' in text() and '回到最新消息' not in text())
        save('submitted-screen')

        # D12 keeps the detach policy at a wide width; a long branch can take
        # priority over that optional label once the terminal narrows.
        until(lambda: '退出暂停' in text())
        # Outside a Git repository (the runner's output directory) there is no branch to show.
        branch = subprocess.run(['git', 'branch', '--show-current'], cwd=state()['cwd'], capture_output=True, text=True).stdout.strip()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 24, 48, 0, 0))
        screen.resize(24, 48)
        os.killpg(child.pid, signal.SIGWINCH)
        # D12: at 48 columns /goal keeps the directory on its label's line, eliding the
        # middle of the path, and the editor hint still fits without truncation.
        send('/goal')
        leaf = Path(state()['cwd']).name
        until(lambda: any(line.startswith('目录：') for line in screen.display))
        directory = next(line.rstrip() for line in screen.display if line.startswith('目录：'))
        assert directory.endswith(leaf) and '…' in directory, screen.display
        until(lambda: HINT in text())
        save('narrow-goal-screen')
        send('/help')
        until(lambda: all(part in text() for part in ['/goal pause | resume | clear', '/quit', '可用命令', '/agents', '/trace']))
        footer = next(line.rstrip() for line in reversed(screen.display) if line.strip())
        assert footer.endswith('/help'), footer
        if branch: assert f'({branch})' in footer, footer
        # Zero counters are not shown; every rendered row fits the 48-column terminal.
        assert '待处理 0' not in text() and '预留 0' not in text()
        assert all(len(line) <= 48 for line in screen.display), screen.display
        assert 'controlled' not in '\n'.join(screen.display[-5:])
        save('narrow-help-screen')
        final = state()
        assert b'Invocation stopped by host' not in transcript
        assert b'"decision_id"' not in transcript, 'internal Goal scheduling JSON leaked into the UI'
        os.write(master, b'/quit\r')
        until(lambda: child.poll() is not None)
        assert child.returncode == 0
        (output / 'scenario.json').write_text(json.dumps({'status': 'passed', 'final': final,
            'checks': ['startup_tail', 'editor_hint', 'go_tab_completion', 'codex_goal_lifecycle', 'visible_rejection',
                'latest_command', 'submit_from_history', 'narrow_goal_path', 'narrow_footer', 'no_synthetic_final_answer']}, indent=2))
        print(json.dumps({'status': 'passed', 'spent': final['budget']['spent']}))
    finally:
        (output / 'pty-transcript.txt').write_bytes(transcript)
        (output / 'last-screen.txt').write_text(text())
        (output / 'model-requests.json').write_text(json.dumps(Provider.requests, indent=2))
        stop_child(child)
        if client: client.close()
        if host_pid:
            try: os.kill(host_pid, signal.SIGTERM)
            except ProcessLookupError: pass
        os.close(master); os.close(slave)
        provider.shutdown(); provider.server_close()


if __name__ == '__main__': main()
