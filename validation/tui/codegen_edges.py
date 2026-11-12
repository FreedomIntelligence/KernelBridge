"""PTY checks for setup, initial prompts and explicit budget exhaustion."""
import argparse
import fcntl
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import pty
import select
import struct
import subprocess
import shutil
import sys
import termios
import threading
import time
import uuid

import pyte
from codegen_fixture import CodegenProvider
from product_scenario import ROOT, cleanup_fixture_hosts
from pty_scenario import Client, stop_child
from timing import SCALE, StepTimeout, slow


class Terminal:
    def __init__(self, command, cwd, env):
        self.master, self.slave = pty.openpty()
        fcntl.ioctl(self.slave, termios.TIOCSWINSZ, struct.pack('HHHH', 30, 100, 0, 0))
        self.screen = pyte.Screen(100, 30); self.stream = pyte.ByteStream(self.screen); self.transcript = bytearray()
        self.child = subprocess.Popen(command, cwd=cwd, env=env, stdin=self.slave, stdout=self.slave, stderr=self.slave, start_new_session=True)

    def text(self): return '\n'.join(self.screen.display)
    def send(self, text): os.write(self.master, text.encode() + b'\r')
    def pump(self):
        if select.select([self.master], [], [], .1)[0]:
            try: data = os.read(self.master, 65536)
            except OSError: return
            self.transcript.extend(data); self.stream.feed(data)
            if b'\x1b[6n' in data: os.write(self.master, b'\x1b[1;1R')
    def until(self, predicate, timeout=90):
        """Wait for predicate; the base timeout is scaled by KB_TIMEOUT_SCALE and reported as SLOW, then TIMEOUT."""
        start = last_output = time.monotonic(); deadline = start + timeout * SCALE; warned = False
        while time.monotonic() < deadline:
            before = len(self.transcript)
            self.pump()
            now = time.monotonic()
            if len(self.transcript) != before: last_output = now
            if predicate(): return
            if self.child.poll() is not None: raise AssertionError(self.text())
            if not warned and now - start > timeout:
                warned = True; slow(f'step past its {timeout}s base deadline (scale {SCALE}); last PTY output {now - last_output:.1f}s ago')
        raise StepTimeout(f'TIMEOUT after {timeout * SCALE:.0f}s (base {timeout}s x scale {SCALE}); '
                          f'last PTY output {time.monotonic() - last_output:.1f}s ago\n' + self.text())
    def close(self, output):
        stop_child(self.child)
        output.write_bytes(self.transcript)
        os.close(self.master); os.close(self.slave)


def main():
    parser = argparse.ArgumentParser(); parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--launcher', type=Path); parser.add_argument('--deployment', type=Path)
    args = parser.parse_args(); output = args.output.resolve(); output.mkdir(parents=True, exist_ok=False)
    if not args.launcher and not args.deployment: parser.error('--launcher or --deployment is required')
    launcher = [str(args.launcher.resolve())] if args.launcher else [shutil.which('node'), str(ROOT / 'source/apps/agent/src/main.ts'),
        '--python', sys.executable, '--expected-python-modules', str(args.deployment.resolve() / 'expected.json')]
    project = output / 'project'; project.mkdir(); (project / 'solution.py').write_text('x = 1\n')
    home = output / 'home'
    env = {**os.environ, 'TERM': 'xterm-256color', 'KERNELBRIDGE_HOME': str(home),
        'KERNELBRIDGE_CONFIG': str(output / 'empty.json'), 'KERNELBRIDGE_SESSIONS': str(Path('/tmp').resolve() / ('kb-edge-' + uuid.uuid4().hex[:10]))}
    (output / 'empty.json').write_text('{}')
    if args.deployment: env['PYTHONPATH'] = str(args.deployment.resolve() / 'unpacked')
    checks = []
    ui = Terminal(launcher, project, env)
    try:
        ui.until(lambda: '等待输入' in ui.text()); ui.send('hello')
        ui.until(lambda: '尚未配置模型' in ui.text())
        assert not home.exists() and (project / 'solution.py').read_text() == 'x = 1\n'
        os.write(ui.master, b'\x03'); ui.send('/help'); ui.until(lambda: '/config PATH' in ui.text())
        os.write(ui.master, b'\x04'); ui.until(lambda: ui.child.poll() is not None)
        assert ui.child.returncode == 0
        checks.append('missing_model_idle_help_clear_exit')
    finally: ui.close(output / 'setup-transcript.txt')
    server = ThreadingHTTPServer(('127.0.0.1', 0), CodegenProvider)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    config = output / 'model.json'; config.write_text(json.dumps({'providers': {'primary': {'model': 'budget-fixture',
        'base_url': f'http://127.0.0.1:{server.server_port}/v1', 'api_key_env': 'KERNELBRIDGE_TEST_KEY'}}}))
    env.update(KERNELBRIDGE_CONFIG=str(config), KERNELBRIDGE_TEST_KEY='fixture-not-secret')
    ui = Terminal([*launcher, '--budget', '1', 'Read and edit solution.py.'], project, env)
    client = None
    try:
        ui.until(lambda: bool(list(home.glob('runs/*/application.json'))))
        binding = json.loads(next(home.glob('runs/*/application.json')).read_text())
        directory = Path(binding['host_directory']); ui.until(lambda: (directory / 'host.sock').exists())
        client = Client(directory)
        ui.until(lambda: any(item['status'] == 'failed' for item in client.call('status')['inputs'].values()))
        ui.until(lambda: '显式设置的执行上限' in ui.text())
        state = client.call('status')
        assert state['budget'] == {'limit': 1, 'spent': 1, 'reserved': 0, 'resources': None}, state['budget']
        assert len(CodegenProvider.requests) == 1 and not state['pending_effect_ids']
        assert (project / 'solution.py').read_text() == 'x = 1\n'
        assert '预算 1/1' in ui.text() and 'Trial' not in ui.text()
        ui.send('/quit'); ui.until(lambda: ui.child.poll() is not None); assert ui.child.returncode == 0
        checks.append('explicit_budget_initial_prompt_no_overspend')
    finally:
        ui.close(output / 'budget-transcript.txt')
        if client: client.close()
        for run in home.glob('runs/*'): cleanup_fixture_hosts(run)
        server.shutdown()
    (output / 'scenario.json').write_text(json.dumps({'status': 'passed', 'checks': checks}, indent=2))
    print(json.dumps({'status': 'passed', 'checks': checks}))


if __name__ == '__main__': main()
