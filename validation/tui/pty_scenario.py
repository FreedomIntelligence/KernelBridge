"""Real PTY acceptance of the shipped CLI with deterministic local tools/provider."""
import argparse
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pty
import select
import shutil
import signal
import socket
import struct
import subprocess
import sys
import termios
import threading
import time
from http.server import ThreadingHTTPServer
from product_scenario import ROOT, Provider, prepare
from timing import SCALE, StepTimeout


class Client:
    def __init__(self, directory):
        self.socket = socket.socket(socket.AF_UNIX)
        self.socket.settimeout(10 * SCALE)
        self.socket.connect(str(directory / 'host.sock'))
        self.file = self.socket.makefile('rwb', buffering=0)
        self.counter = 0
        self.call('authenticate', {'token': (directory / 'token').read_text()})
    def call(self, method, params=None):
        self.counter += 1
        self.file.write((json.dumps({'id': self.counter, 'method': method, 'params': params or {}}) + '\n').encode())
        while True:
            response = json.loads(self.file.readline())
            if response.get('id') == self.counter:
                if 'error' in response: raise RuntimeError(response['error'])
                return response['result']
    def close(self):
        self.file.close()
        self.socket.close()


def stop_child(child):
    """Reap only this fixture's process group, even if terminal shutdown stalls."""
    if child.poll() is None:
        os.killpg(child.pid, signal.SIGTERM)
    try:
        child.wait(timeout=10 * SCALE)
    except subprocess.TimeoutExpired:
        os.killpg(child.pid, signal.SIGKILL)
        child.wait(timeout=10 * SCALE)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--deployment', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--launcher', type=Path, help='exercise an installed kernelagent wrapper')
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists(): raise ValueError('choose a new output directory')
    provider = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
    threading.Thread(target=provider.serve_forever, daemon=True).start()
    options = prepare(output, args.deployment.resolve(), f'http://127.0.0.1:{provider.server_port}/v1', name='tui')
    registry = Path('/tmp').resolve() / ('kb-reg-' + hashlib.sha256(str(output).encode()).hexdigest()[:10])
    env = {**os.environ, 'TERM': 'xterm-256color', 'PI_OFFLINE': '1', 'KERNELBRIDGE_TEST_KEY': 'fixture-key-not-a-real-credential',
        'PYTHONPATH': str(args.deployment.resolve() / 'unpacked'), 'KERNELBRIDGE_SESSIONS': str(registry)}
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 30, 100, 0, 0))
    launcher = [str(args.launcher.resolve())] if args.launcher else [shutil.which('node'), str(ROOT / 'source/apps/agent/src/main.ts')]
    command = [*launcher, 'tui', '--task', options['task'],
        '--config', options['config'], '--output', str(output), '--python', sys.executable,
        '--expected-python-modules', options['expectedPythonModules'], '--session-only']
    child = subprocess.Popen(command, env=env, stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
    transcript = bytearray()
    client = None
    host_pid = None
    def drain():
        if select.select([master], [], [], .05)[0]:
            try:
                data = os.read(master, 65536)
                transcript.extend(data)
                if b'\x1b[6n' in data: os.write(master, b'\x1b[1;1R')
            except OSError: pass
    def until(predicate, timeout=30):
        deadline = time.monotonic() + timeout * SCALE
        while time.monotonic() < deadline:
            drain()
            if predicate(): return
            if child.poll() is not None: raise AssertionError(f'CLI exited {child.returncode}: {transcript[-4000:]!r}')
        raise StepTimeout(f'TIMEOUT after {timeout * SCALE:.0f}s (base {timeout}s x scale {SCALE}): {transcript[-4000:]!r}')
    def completed(text):
        return any(item['text'] == text and item['status'] == 'completed' for item in client.call('status')['inputs'].values())
    def send(text):
        os.write(master, text.encode() + b'\r')
        until(lambda: completed(text))
    def terminal_restored():
        assert b'\x1b[?2004l' in transcript and b'\x1b[?25h' in transcript
        try:
            assert termios.tcgetattr(slave)[3] & termios.ICANON
        except termios.error as error:
            # macOS can hang up the old slave when the session leader exits.
            # The reset sequence was observed; attach gets a new terminal below.
            if sys.platform != 'darwin' or error.args[0] not in {errno.ENOTTY, errno.EIO}: raise
    try:
        until(lambda: (output / 'application.json').exists())
        directory = Path(json.loads((output / 'application.json').read_text())['host_directory'])
        until(lambda: (directory / 'host.sock').exists())
        client = Client(directory)
        host_pid = client.call('status')['host_pid']
        until(lambda: any(item['status'] == 'completed' and item['kind'] == 'prompt' for item in client.call('status')['inputs'].values()))
        initial = client.call('status')
        assert initial['budget']['spent'] == 15, initial['budget']
        assert initial['evidence'][0]['evidence_tier'] == 'E1'
        send('!printf user-bash')
        send('!!printf hidden-bash')
        # Bracketed paste exercises UTF-8 and multiline editor semantics.
        text = '中文测试\nsecond line'
        os.write(master, b'\x1b[200~' + text.encode() + b'\x1b[201~\r')
        until(lambda: completed(text))
        send('retain the Chinese constraint after the long output')
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 20, 48, 0, 0))
        os.killpg(child.pid, signal.SIGWINCH)
        send('/reload')
        send('/model primary')
        send('/compact')
        assert any('/compact/' in row.get('action', {}).get('id', '') for row in
            map(json.loads, (output / 'state/events.jsonl').read_text().splitlines()))
        until(lambda: not client.call('status')['pending_effect_ids'])
        send('/context reset')
        send('after context reset')
        state = client.call('status')
        assert len(state['trials']) == 2, state['trials']
        assert state['budget']['spent'] > initial['budget']['spent']
        os.write(master, b'/quit\r')
        until(lambda: child.poll() is not None)
        assert child.returncode == 0
        terminal_restored()
        assert b'kernelagent tui --attach ' + state['application_session_id'].encode() in transcript
        deadline = time.monotonic() + 5
        while client.call('status')['lease'] and time.monotonic() < deadline: time.sleep(.05)
        assert client.call('status')['lease'] is None
        first_transcript_length = len(transcript)
        os.close(master)
        os.close(slave)
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 24, 80, 0, 0))
        child = subprocess.Popen([*launcher, 'tui', '--attach', state['application_session_id']],
            env=env, stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
        # Raw mode is enabled before Pi installs its submit handler. The host
        # footer is mounted during extension binding, after that handler exists.
        until(lambda: b'Session \xc2\xb7 task v1' in transcript[first_transcript_length:])
        send('Continue in the reattached native TUI.')
        attached = client.call('status')
        assert attached['checkpoint']['pi_session_id'] == state['checkpoint']['pi_session_id']
        assert len(attached['trials']) == 2
        os.write(master, b'/quit\r')
        until(lambda: child.poll() is not None)
        assert child.returncode == 0
        terminal_restored()
        (output / 'scenario.json').write_text(json.dumps({'status': 'passed', 'initial': initial, 'final': state, 'attached': attached}, indent=2))
        print(json.dumps({'status': 'passed', 'spent': state['budget']['spent'], 'trials': len(state['trials'])}))
    finally:
        (output / 'pty-transcript.txt').write_bytes(transcript)
        (output / 'model-requests.json').write_text(json.dumps(Provider.requests, indent=2))
        stop_child(child)
        if client: client.close()
        if host_pid:
            try: os.kill(host_pid, signal.SIGTERM)
            except ProcessLookupError: pass
        os.close(master)
        os.close(slave)
        provider.shutdown()


if __name__ == '__main__': main()
