"""End-to-end check of the pi-lattice-trace plugin in a real Pi 1.5.0 process (RPC mode).

A scripted OpenAI-compatible provider drives three turns (parallel read + write,
a slow bash command, an edit). The plugin is loaded with `-e`; checks cover the
served trace, a running tool, SSE growth, a headless Chrome render, /new on the
same URL, and the server stopping when Pi exits. Pi runs with an isolated
PI_CODING_AGENT_DIR, so the user's ~/.pi is never read or written.
"""
import argparse, json, os, re, socket, subprocess, sys, tempfile, threading, time, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PLUGIN = ROOT / 'source/packages/pi-lattice-trace'
PI = ROOT / 'source/apps/agent/node_modules/.bin/pi'


class Provider(BaseHTTPRequestHandler):
    """Turn script keyed by the last message: user prompt -> tool calls, tool results -> final text."""
    calls = []

    def log_message(self, *args): pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])) or b'{}')
        messages = body.get('messages', []); last = messages[-1] if messages else {}
        text = json.dumps(last.get('content', ''))
        prompt = 1000 + 150 * len(messages)
        if last.get('role') == 'user' and 'make greet' in text:
            calls = [('read', {'path': 'hello.txt'}), ('write', {'path': 'greet.py', 'content': 'def greet():\n    return "hi"\n'})]
        elif last.get('role') == 'user' and 'run something slow' in text:
            calls = [('bash', {'command': 'sleep 3; echo slow-done'})]
        elif last.get('role') == 'user' and 'now edit' in text:
            calls = [('edit', {'path': 'greet.py', 'edits': [{'oldText': '"hi"', 'newText': '"hello"'}]})]
        else:
            calls = []
        Provider.calls.append({'messages': len(messages), 'tools': [c[0] for c in calls], 'prompt_tokens': prompt})
        self.send_response(200); self.send_header('Content-Type', 'text/event-stream'); self.end_headers()
        base = {'id': 'c%d' % len(Provider.calls), 'object': 'chat.completion.chunk', 'created': 1, 'model': 'fixture-1'}
        def send(obj): self.wfile.write(b'data: ' + json.dumps(obj).encode() + b'\n\n'); self.wfile.flush()
        time.sleep(0.3)
        delta = {'role': 'assistant', 'content': 'Working on it.' if calls else 'Done.'}
        if calls:
            delta['tool_calls'] = [{'index': i, 'id': 'call_%d_%d' % (len(Provider.calls), i), 'type': 'function',
                                    'function': {'name': name, 'arguments': json.dumps(args)}} for i, (name, args) in enumerate(calls)]
        send({**base, 'choices': [{'index': 0, 'delta': delta}]})
        send({**base, 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'tool_calls' if calls else 'stop'}],
              'usage': {'prompt_tokens': prompt, 'completion_tokens': 20, 'prompt_tokens_details': {'cached_tokens': prompt // 2}}})
        self.wfile.write(b'data: [DONE]\n\n'); self.wfile.flush()


class Rpc:
    def __init__(self, cwd, env, log):
        self.events, self.lock = [], threading.Lock()
        self.process = subprocess.Popen([str(PI), '--mode', 'rpc', '-e', str(PLUGIN), '--model', 'fixture/fixture-1'], cwd=cwd, env=env,
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=open(log.with_suffix('.err'), 'w'), text=True)
        self.log = open(log, 'w')
        threading.Thread(target=self.read, daemon=True).start()

    def read(self):
        for line in self.process.stdout:
            self.log.write(line); self.log.flush()
            try: event = json.loads(line)
            except ValueError: continue
            with self.lock: self.events.append(event)

    def send(self, command):
        self.process.stdin.write(json.dumps(command) + '\n'); self.process.stdin.flush()

    def wait(self, predicate, timeout=60, what='condition'):
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self.lock: found = [e for e in self.events if predicate(e)]
            if found: return found
            if self.process.poll() is not None: raise AssertionError('pi exited while waiting for ' + what)
            time.sleep(0.05)
        raise AssertionError('timed out waiting for ' + what)

    def turn(self, message, timeout=60):
        with self.lock: start = len(self.events)
        self.send({'type': 'prompt', 'message': message})
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self.lock:
                if any(e.get('type') == 'agent_settled' for e in self.events[start:]): return
            time.sleep(0.05)
        raise AssertionError('turn did not settle: ' + message)


def fetch(url, path):
    with urllib.request.urlopen(url.split('#')[0] + path, timeout=10) as response: return json.loads(response.read())


class Sse(threading.Thread):
    def __init__(self, url):
        super().__init__(daemon=True); self.url, self.traces = url.split('#')[0] + 'events', []

    def run(self):
        with urllib.request.urlopen(self.url, timeout=120) as stream:
            data = None
            for raw in stream:
                line = raw.decode().rstrip('\n')
                if line.startswith('data: '): data = line[6:]
                elif not line and data: self.traces.append(json.loads(data)); data = None


def browser_check(url, output):
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
        texts = []
        for tab in ['timeline', 'overview', 'context', 'history', 'workspace', 'decisions', 'files', 'compare', 'agent', 'live-progress', 'timeline']:
            page.evaluate(f'setTab({json.dumps(tab)})'); page.wait_for_timeout(250); texts.append(page.evaluate('document.body.innerText'))
        state = page.evaluate('({steps: STEPS.length, live: !!DS.live, crumbs: document.getElementById("crumbs").innerText, title: document.title})')
        page.screenshot(path=str(output / 'pi-trace-viewer.png'))
        browser.close()
    assert not errors, errors
    assert state['live'] and state['steps'] >= 8 and state['title'] == 'Pi · 实时轨迹', state
    assert 'Pi' in state['crumbs'] and '● 实时' in state['crumbs'] and 'KernelBridge' not in state['crumbs'], state
    joined = '\n'.join(texts)
    assert 'Harness · Pi' in joined and '会话记录（只读）' in joined, 'Pi labels missing from the rendered page'
    assert 'KernelBridge' not in joined, 'KernelBridge wording leaked into the Pi page'
    return 'browser_renders_pi_session_without_errors'


def main():
    parser = argparse.ArgumentParser(); parser.add_argument('--output', type=Path, required=True)
    output = parser.parse_args().output.resolve(); output.mkdir(parents=True, exist_ok=True)
    provider = ThreadingHTTPServer(('127.0.0.1', 0), Provider); threading.Thread(target=provider.serve_forever, daemon=True).start()
    work = Path(tempfile.mkdtemp(prefix='pi-trace-'))
    agent, project = work / 'agent', work / 'project'; agent.mkdir(); project.mkdir()
    (project / 'hello.txt').write_text('hello-world\n')
    (agent / 'models.json').write_text(json.dumps({'providers': {'fixture': {'baseUrl': f'http://127.0.0.1:{provider.server_port}/v1', 'api': 'openai-completions',
        'apiKey': 'fixture', 'models': [{'id': 'fixture-1', 'name': 'fixture-1', 'contextWindow': 64000, 'maxTokens': 4096,
        'cost': {'input': 0, 'output': 0, 'cacheRead': 0, 'cacheWrite': 0}}]}}}))
    env = {**os.environ, 'PI_CODING_AGENT_DIR': str(agent), 'PI_LATTICE_NO_BROWSER': '1'}
    env.pop('SSH_CONNECTION', None); env.pop('SSH_TTY', None)
    checks = []
    rpc = Rpc(project, env, output / 'rpc-out.jsonl')
    try:
        rpc.turn('Read hello.txt and make greet.py')
        rpc.send({'type': 'prompt', 'message': '/trace --no-open'})
        notice = rpc.wait(lambda e: e.get('type') == 'extension_ui_request' and e.get('method') == 'notify' and '实时轨迹' in e.get('message', ''), what='trace notice')[0]
        url = re.search(r'http://127\.0\.0\.1:\d+/[0-9a-f]{32}/#invoice--timeline', notice['message']).group(0)
        port = int(re.search(r':(\d+)/', url).group(1))
        trace = fetch(url, 'trace.json')
        assert trace['schema'] == 'kernelbridge.trace/v1' and trace['labels']['harness'] == 'Pi', trace.get('labels')
        tools = {s.get('tool'): s for s in trace['steps'] if s['src'] == 'tool'}
        assert tools['read']['status'] == 'ok' and 'hello-world' in tools['read']['out'], tools.get('read')
        assert tools['write']['status'] == 'ok' and tools['write'].get('commit') == 'E01', tools.get('write')
        models = [s for s in trace['steps'] if s['src'] == 'model']
        assert [m['tokIn'] for m in models] == [c['prompt_tokens'] for c in Provider.calls[:len(models)]], (models, Provider.calls)
        assert models[0]['cacheReal'] == Provider.calls[0]['prompt_tokens'] // 2
        session_id = trace['session']['id']; assert session_id and trace['session']['cwd'] == str(project.resolve()) or trace['session']['cwd'] == str(project)
        checks.append('slash_trace_serves_this_pi_session')

        sse = Sse(url); sse.start()
        with rpc.lock: slow_start = len(rpc.events)
        rpc.send({'type': 'prompt', 'message': 'run something slow'})
        deadline = time.time() + 30; running = None
        while time.time() < deadline and not running:
            running = next((s for s in fetch(url, 'trace.json')['steps'] if s.get('tool') == 'bash' and s['status'] == 'running'), None)
            time.sleep(0.2)
        assert running, 'the executing bash call never appeared as running'
        deadline = time.time() + 40
        while time.time() < deadline:
            with rpc.lock: settled = any(e.get('type') == 'agent_settled' for e in rpc.events[slow_start:])
            if settled: break
            time.sleep(0.05)
        assert settled, 'slow turn did not settle'
        assert any(s.get('tool') == 'bash' and s['status'] == 'ok' and 'slow-done' in s['out'] for s in fetch(url, 'trace.json')['steps'])
        checks.append('executing_tool_is_shown_running')

        rpc.turn('now edit greet.py')
        deadline = time.time() + 15
        while time.time() < deadline and not any(t['commits'] and len(t['commits']) >= 2 for t in sse.traces): time.sleep(0.1)
        latest = sse.traces[-1]
        assert len(latest['commits']) == 2 and latest['files']['greet.py'][1][0] == '    return "hello"', latest['files']
        assert len(sse.traces) >= 3, len(sse.traces)
        checks.append('sse_pushes_each_turn')

        checks.append(browser_check(url, output))

        rpc.send({'type': 'new_session'})
        rpc.wait(lambda e: e.get('type') == 'response' and e.get('command') == 'new_session', what='new session')
        deadline = time.time() + 10; fresh = None
        while time.time() < deadline:
            fresh = fetch(url, 'trace.json')
            if fresh['session']['id'] != session_id: break
            time.sleep(0.1)
        assert fresh['session']['id'] != session_id and not fresh['steps'], fresh['session']
        checks.append('new_session_reuses_the_same_url')
    finally:
        rpc.process.stdin.close()
        try: rpc.process.wait(timeout=20)
        except subprocess.TimeoutExpired: rpc.process.kill(); raise AssertionError('pi did not exit after stdin closed')
        provider.shutdown()
    if 'new_session_reuses_the_same_url' in checks:
        with socket.socket() as probe:
            refused = probe.connect_ex(('127.0.0.1', port)) != 0
        assert refused, 'trace server still listening after Pi exited'
        checks.append('server_stops_when_pi_exits')
    (output / 'results.json').write_text(json.dumps({'checks': checks, 'provider_calls': Provider.calls}, indent=2))
    print('\n'.join(checks)); print(f'{len(checks)} checks passed')


if __name__ == '__main__':
    sys.exit(main())
