"""PTY acceptance of the live trace: /trace in a real session, SSE growth, browser render, shutdown and the standalone command."""
import argparse
from http.server import ThreadingHTTPServer
import http.client
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid

from advanced_scenario import Provider
from codegen_edges import Terminal
from pty_scenario import Client

URL = re.compile(rb'http://127\.0\.0\.1:(\d+)/([0-9a-f]{32})/')


def fetch(port, token, path=''):
    with urllib.request.urlopen(f'http://127.0.0.1:{port}/{token}/{path}', timeout=10) as response:
        return response.status, response.read().decode()


class Events(threading.Thread):
    """Collects trace snapshots from the SSE endpoint."""
    def __init__(self, port, token):
        super().__init__(daemon=True); self.port, self.token, self.traces = port, token, []
    def run(self):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=120)
        connection.request('GET', f'/{self.token}/events'); response = connection.getresponse(); frame = []
        while True:
            line = response.fp.readline()
            if not line: return
            line = line.decode().rstrip('\n')
            if line: frame.append(line); continue
            data = next((item[6:] for item in frame if item.startswith('data: ')), None)
            if 'event: trace' in frame and data: self.traces.append(json.loads(data))
            frame = []


def browser_check(url, expected_steps, output):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return 'browser_skipped_no_playwright'
    errors = []
    with sync_playwright() as playwright:
        try: browser = playwright.chromium.launch(channel='chrome', headless=True)
        except Exception:
            try: browser = playwright.chromium.launch(headless=True)
            except Exception: return 'browser_skipped_no_chromium'
        page = browser.new_page(viewport={'width': 1440, 'height': 900})
        page.on('pageerror', lambda error: errors.append(str(error)))
        # The viewer catches render errors itself and logs them; they count as failures too.
        page.on('console', lambda message: errors.append(message.text) if message.type == 'error' and 'Failed to load resource' not in message.text else None)
        page.goto(url); page.wait_for_function('typeof STEPS !== "undefined" && STEPS.length > 0', timeout=15000)
        for tab in ['timeline', 'overview', 'context', 'history', 'workspace', 'decisions', 'files', 'compare', 'agent', 'timeline']:
            page.evaluate(f'setTab({json.dumps(tab)})'); page.wait_for_timeout(250)
        state = page.evaluate('({steps: STEPS.length, rows: ROWS.slice(), live: !!DS.live, title: document.getElementById("sbTitle").textContent, crumbs: document.getElementById("crumbs").innerText})')
        page.screenshot(path=str(output / 'trace-viewer.png'))
        browser.close()
    assert not errors, errors
    assert state['live'] and state['steps'] >= expected_steps and len(state['rows']) >= 3, state
    assert '● 实时' in state['crumbs'], state
    return 'browser_renders_live_session_without_errors'


def main():
    parser = argparse.ArgumentParser(); parser.add_argument('--output', type=Path, required=True); parser.add_argument('--launcher', type=Path, required=True)
    args = parser.parse_args(); output = args.output.resolve(); output.mkdir(parents=True, exist_ok=False)
    project = output / 'project'; project.mkdir(); (project / 'solution.py').write_text('x = 1\n')
    (project / 'AGENTS.md').write_text('Delegate only if explicitly requested.\n')
    server = ThreadingHTTPServer(('127.0.0.1', 0), Provider); threading.Thread(target=server.serve_forever, daemon=True).start()
    config = output / 'config.json'; config.write_text(json.dumps({'providers': {'primary': {'model': 'trace-fixture',
        'base_url': f'http://127.0.0.1:{server.server_port}/v1', 'api_key_env': 'KERNELBRIDGE_TEST_KEY'}}, 'subagents': {'max_concurrent': 2}}))
    home = output / 'home'; registry = Path('/tmp').resolve() / ('kb-trace-' + uuid.uuid4().hex[:8])
    env = {**os.environ, 'TERM': 'xterm-256color', 'KERNELBRIDGE_HOME': str(home), 'KERNELBRIDGE_SESSIONS': str(registry),
           'KERNELBRIDGE_CONFIG': str(config), 'KERNELBRIDGE_TEST_KEY': 'local-protocol-fixture', 'KERNELBRIDGE_NO_BROWSER': '1'}
    checks = []; ui = Terminal([str(args.launcher.resolve())], project, env); client = None
    try:
        ui.until(lambda: '等待输入' in ui.text())
        binding = json.loads(sorted(home.glob('runs/*/application.json'))[-1].read_text())
        client = Client(Path(binding['host_directory']))
        status = lambda: client.call('status')
        identity = status()['application_session_id']
        def send(text):
            before = set(status()['inputs']); ui.send(text)
            ui.until(lambda: any(k not in before and v['text'] == text and v['status'] in {'completed', 'failed', 'interrupted'} for k, v in status()['inputs'].items()))
            ui.until(lambda: not status()['calls'] and not status()['pending_effect_ids'])
        send('READ_STEP'); send('Please use subagents: DELEGATE_TWO')

        mark = len(ui.transcript); ui.send('/trace')
        ui.until(lambda: URL.search(bytes(ui.transcript[mark:])) is not None)
        found = URL.search(bytes(ui.transcript[mark:])); port, token = int(found[1]), found[2].decode()
        assert '实时轨迹' in ui.text(), ui.text()
        code, page = fetch(port, token); assert code == 200 and 'kernelbridge live adapter' in page
        trace = json.loads(fetch(port, token, 'trace.json')[1])
        assert trace['session']['id'] == identity and trace['schema'] == 'kernelbridge.trace/v1', trace['session']
        assert any(step.get('tool') == 'read' for step in trace['steps'])
        assert [step['kind'] for step in trace['steps'] if step.get('tool') == 'delegate'].count('return') >= 2, trace['steps']
        assert len(trace['rows']) >= 3 and all(trace['agents'][row]['short'] for row in trace['rows'][1:]), trace['rows']
        for bad in (f'/{"0" * 32}/', '/', f'/{token}/../'):
            try: urllib.request.urlopen(f'http://127.0.0.1:{port}{bad}', timeout=5); raise AssertionError(bad)
            except urllib.error.HTTPError as error: assert error.code == 404
        checks.append('slash_trace_serves_this_session_with_children')

        events = Events(port, token); events.start()
        ui.until(lambda: len(events.traces) >= 1); initial = len(events.traces[-1]['steps'])
        mark = len(ui.transcript); ui.send('/trace'); ui.until(lambda: URL.search(bytes(ui.transcript[mark:])) is not None)
        assert int(URL.search(bytes(ui.transcript[mark:]))[1]) == port, 'one viewer per interactive session'
        send('READ_STEP')
        ui.until(lambda: any(len(item['steps']) > initial for item in events.traces))
        assert events.traces[-1]['session']['status'] in {'idle', 'running'}
        checks.append('sse_follows_new_turns_live')

        checks.append(browser_check(f'http://127.0.0.1:{port}/{token}/#invoice--timeline', initial, output))

        ui.send('/quit'); ui.until(lambda: ui.child.poll() is not None); assert ui.child.returncode == 0
        try: fetch(port, token); raise AssertionError('trace server must stop with the interactive session')
        except (urllib.error.URLError, ConnectionError): pass
        checks.append('viewer_stops_with_session')
    finally:
        if client: client.close()
        ui.close(output / 'trace-transcript.txt')

    standalone = subprocess.Popen([str(args.launcher.resolve()), 'trace', '--no-open', identity], cwd=project, env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    try:
        line = standalone.stdout.readline(); found = URL.search(line); assert found, (line, standalone.stderr.read() if standalone.poll() is not None else '')
        trace = json.loads(fetch(int(found[1]), found[2].decode(), 'trace.json')[1])
        assert trace['session']['id'] == identity and len(trace['steps']) > initial, (trace['session'], len(trace['steps']))
        os.killpg(standalone.pid, signal.SIGINT); assert standalone.wait(15) == 0
        checks.append('standalone_trace_command_reads_saved_session')
    finally:
        if standalone.poll() is None: os.killpg(standalone.pid, signal.SIGKILL)
    print(json.dumps({'status': 'passed', 'checks': checks}, ensure_ascii=False))


if __name__ == '__main__': main()
