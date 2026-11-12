"""Real Pi at 48 columns: three background results, live panel, original-input recovery."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import threading
import time
import uuid

from product_scenario import ROOT, cleanup_fixture_hosts
from pty_scenario import Client
from subagent_parallel import Terminal


class Provider(BaseHTTPRequestHandler):
    release = {label: threading.Event() for label in 'ABC'}
    counts = {label: 0 for label in 'ABC'}
    requests = []

    def log_message(self, *_): pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.requests.append(body)
        names = {tool['function']['name'] for tool in body.get('tools', [])}
        latest = next((message.get('content', '') for message in reversed(body['messages']) if message['role'] == 'user'), '')
        text = json.dumps(latest, ensure_ascii=False)
        calls, content = [], 'PARENT_DONE'
        if 'read_file' in names and 'read' not in names:
            label = re.search(r'CHILD_([ABC])', text)[1]
            step = self.counts[label]; self.counts[label] += 1
            if step == 0: calls = [('read_file', {'path': 'solution.py'})]
            else:
                if not self.release[label].wait(90): raise TimeoutError('fixture release not delivered')
                content = f'REPORT_{label} 原始证据：solution.py:1 定义 add。'
        elif '[子代理结果]' in text:
            content = 'NOTIFICATION_READ 已读取此子代理的原始报告。'
        elif 'THREE_BACKGROUND' in text:
            after = []
            for message in reversed(body['messages']):
                if message['role'] == 'user': break
                after.append(message)
            if any(message['role'] == 'tool' for message in after):
                content = 'BACKGROUND_STARTED 三路搜索已启动，结果回来后汇总。'
            else:
                calls = [('delegate', {'agent': 'kernel-scout', 'description': f'检索 {label}',
                                      'task': f'CHILD_{label} read solution.py and report evidence', 'background': True}) for label in 'ABC']
        elif 'AFTER_RESUME' in text:
            content = 'RESUMED_ONCE'
        wire = [{'id': f'call-{time.monotonic_ns()}-{index}', 'type': 'function',
                 'function': {'name': name, 'arguments': json.dumps(arguments, ensure_ascii=False)}} for index, (name, arguments) in enumerate(calls)]
        message = {'role': 'assistant', 'content': content}
        if wire: message['tool_calls'] = wire
        usage = {'prompt_tokens': 100, 'completion_tokens': 10}
        self.send_response(200); self.send_header('Content-Type', 'text/event-stream' if body.get('stream') else 'application/json'); self.end_headers()
        try:
            if not body.get('stream'):
                self.wfile.write(json.dumps({'choices': [{'message': message, 'finish_reason': 'tool_calls' if wire else 'stop'}], 'usage': usage}).encode()); return
            chunks = [{'choices': [{'index': 0, 'delta': {'role': 'assistant', 'content': content}}]}]
            if wire: chunks.append({'choices': [{'index': 0, 'delta': {'tool_calls': [dict(call, index=index) for index, call in enumerate(wire)]}}]})
            chunks.append({'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'tool_calls' if wire else 'stop'}], 'usage': usage})
            for chunk in chunks:
                self.wfile.write(('data: ' + json.dumps(chunk) + '\n\n').encode()); self.wfile.flush()
            self.wfile.write(b'data: [DONE]\n\n')
        except (BrokenPipeError, ConnectionResetError): pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--launcher', type=Path, default=ROOT / 'kernelagent')
    args = parser.parse_args(); output = args.output.resolve(); output.mkdir(parents=True, exist_ok=False)
    project = output / 'project'; project.mkdir(); (project / 'solution.py').write_text('def add(a, b):\n    return a + b\n')
    home = output / 'home'
    server = ThreadingHTTPServer(('127.0.0.1', 0), Provider); threading.Thread(target=server.serve_forever, daemon=True).start()
    config = output / 'config.json'; config.write_text(json.dumps({'providers': {'primary': {
        'model': 'panel-fixture', 'base_url': f'http://127.0.0.1:{server.server_port}/v1', 'api_key_env': 'KERNELBRIDGE_TEST_KEY'}}}))
    env = {**os.environ, 'TERM': 'xterm-256color', 'KERNELBRIDGE_HOME': str(home), 'KERNELBRIDGE_CONFIG': str(config),
           'KERNELBRIDGE_SESSIONS': str(Path('/tmp').resolve() / ('kb-panel-' + uuid.uuid4().hex[:8])), 'KERNELBRIDGE_TEST_KEY': 'fixture'}
    ui = Terminal([str(args.launcher.resolve())], project, env, rows=40, columns=48)
    client = None; checks = []

    def settle():
        deadline = time.monotonic() + .5
        while time.monotonic() < deadline: ui.pump()

    def state(): return client.call('status')

    def notifications(): return [row for row in state()['inputs'].values() if row['initiator'] == 'agent']

    def save(name): (output / (name + '-screen.txt')).write_text(ui.text())

    try:
        ui.until(lambda: '/help' in ui.text() and '等待输入' in ui.text())
        startup_text = re.sub(r'\x1b\[[0-9;?]*[ -/]*[@-~]', '', ui.transcript.decode(errors='replace'))
        assert not re.search(r'\d+\.\d+%/\d+[kM]', startup_text), 'native token footer flashed before host state'
        save('startup'); checks.append('first_frame_omits_native_token_budget_footer')
        # Submit at the first ready footer, before extra settling delays.
        ui.send('THREE_BACKGROUND')
        binding = json.loads(next(home.glob('runs/*/application.json')).read_text()); client = Client(Path(binding['host_directory']))
        ui.until(lambda: '等待 3 个子代理完成' in ui.text() and '个运行中 · /agents 查看' in ui.text(), timeout=60)
        assert len(state()['subagents']) == 3
        assert not any(row['status'] == 'completed' for row in state()['subagents'])
        save('three-running'); checks.append('three_background_children_visible_at_48_columns')
        Provider.release['A'].set()
        # After a notification turn the parent is idle: the footer, not the working row, counts running children.
        ui.until(lambda: len(notifications()) == 1 and notifications()[0]['status'] == 'completed'
                 and '子代理 2 个运行中' in ui.text(), timeout=60)
        settle(); save('two-running')
        assert 'kernel-scout「检索 A」已完成' in ui.text(), ui.text()
        ui.send('/agents'); ui.until(lambda: '本会话（3）' in ui.text())
        types = len(state()['subagent_types'])
        for _ in range(types + 1): os.write(ui.master, b'\x1b[B'); settle()
        os.write(ui.master, b'\r'); ui.until(lambda: 'CHILD_B' in ui.text() and '尚无报告' in ui.text())
        Provider.release['B'].set()
        ui.until(lambda: 'REPORT_B' in ui.text() and '完成' in ui.text(), timeout=60)
        save('live-report'); checks.append('open_agent_report_updates_when_background_result_arrives')
        os.write(ui.master, b'\x1b'); ui.until(lambda: '本会话（3）' in ui.text())
        os.write(ui.master, b'\x1b'); ui.until(lambda: '子代理 1 个运行中' in ui.text(), timeout=60)
        ui.until(lambda: len(notifications()) == 2 and all(row['status'] == 'completed' for row in notifications()))
        save('one-running'); Provider.release['C'].set()
        ui.until(lambda: len(notifications()) == 3 and all(row['status'] == 'completed' for row in notifications())
                 and '等待输入' in ui.text(), timeout=60)
        settle(); save('all-completed')
        final = state()
        assert all(child['status'] == 'completed' and child['tool_calls'] == 1 for child in final['subagents'])
        assert not final['pending_effect_ids'] and final['budget']['reserved'] == 0
        assert len([row for row in final['inputs'].values() if row['text'] == 'THREE_BACKGROUND']) == 1
        assert b'durable host receipt' not in ui.transcript
        assert len({row['input_id'] for row in notifications()}) == 3
        checks.append('waiting_count_decrements_3_2_1_and_each_result_notifies_once')
        checks.append('startup_prompt_executes_once_without_durable_receipt_error')
        identity = final['application_session_id']; before = len(Provider.requests)
        ui.send('/quit'); ui.until(lambda: ui.child.poll() is not None); ui.close(output / 'initial-transcript.txt')
        ui = Terminal([str(args.launcher.resolve()), 'resume', identity], project, env, rows=40, columns=48)
        ui.until(lambda: '等待输入' in ui.text()); settle()
        assert len(Provider.requests) == before, 'resume replayed a completed child or notification'
        assert len(notifications()) == 3
        ui.send('AFTER_RESUME'); ui.until(lambda: 'RESUMED_ONCE' in ui.text() and '等待输入' in ui.text())
        assert len(Provider.requests) == before + 1
        assert b'durable host receipt' not in ui.transcript
        save('resumed'); checks.append('resume_does_not_repeat_children_or_completion_inputs')
        ui.send('/quit'); ui.until(lambda: ui.child.poll() is not None)
        (output / 'scenario.json').write_text(json.dumps({'status': 'passed', 'checks': checks, 'model_requests': len(Provider.requests),
            'scope': 'real Pi, scripted local provider, no device'}, ensure_ascii=False, indent=2) + '\n')
        (output / 'final-state.json').write_text(json.dumps(state(), ensure_ascii=False, indent=2) + '\n')
        print(json.dumps({'status': 'passed', 'checks': checks}), flush=True)
    finally:
        for event in Provider.release.values(): event.set()
        save('last')
        ui.close(output / 'last-transcript.txt')
        if client: client.close()
        for run in home.glob('runs/*'): cleanup_fixture_hosts(run)
        server.shutdown(); server.server_close()


if __name__ == '__main__': main()
