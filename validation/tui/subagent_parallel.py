"""Real Pi PTY: concurrent read-only children, live rows, reports and tool-row presentation.

Local scripted SSE provider only; no real model, device or network.
"""
import argparse
from collections import defaultdict
import fcntl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import pty
import re
import struct
import subprocess
import termios
import threading
import time
import uuid

import pyte
import codegen_edges
from product_scenario import ROOT, cleanup_fixture_hosts
from pty_scenario import Client

DELAY = 1.2


class Terminal(codegen_edges.Terminal):
    def __init__(self, command, cwd, env, rows=40, columns=100):
        self.master, self.slave = pty.openpty()
        fcntl.ioctl(self.slave, termios.TIOCSWINSZ, struct.pack('HHHH', rows, columns, 0, 0))
        self.screen = pyte.Screen(columns, rows); self.stream = pyte.ByteStream(self.screen); self.transcript = bytearray()
        self.child = subprocess.Popen(command, cwd=cwd, env=env, stdin=self.slave, stdout=self.slave, stderr=self.slave,
                                      start_new_session=True)


class Provider(BaseHTTPRequestHandler):
    children = defaultdict(int)
    def log_message(self, *_): pass
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        tools = {tool['function']['name'] for tool in body.get('tools', [])}
        latest = next((m.get('content', '') for m in reversed(body['messages']) if m['role'] == 'user'), '')
        text = json.dumps(latest, ensure_ascii=False)
        after_user = []
        for message in reversed(body['messages']):
            if message['role'] == 'user': break
            after_user.append(message)
        answered = any(m['role'] == 'tool' for m in after_user)
        calls, content = [], 'PARENT_DONE'
        if 'read_file' in tools and 'read' not in tools:
            label = (re.search('CHILD_[A-Z]+', text) or ['CHILD_X'])[0]
            step = self.children[label]; self.children[label] += 1
            time.sleep(DELAY)
            plan = [('list_files', {'path': '.', 'pattern': '*.py'}), ('search_text', {'query': 'def '}),
                    ('read_file', {'path': 'solution.py'})]
            if step < len(plan): calls, content = [plan[step]], ''
            else: content = f'{label} 结论：solution.py 定义 add，证据 solution.py:1。'
        elif tools and '[子代理结果]' in text:
            content = 'BACKGROUND_SEEN 已读取后台子代理结果。'
        elif tools and not answered and 'BACKGROUND_START' in text:
            content = '后台启动扫描。'
            calls = [('delegate', {'agent': 'kernel-scout', 'description': '后台扫描', 'task': 'CHILD_BG scan in background', 'background': True})]
        elif tools and answered and 'BACKGROUND_START' in text:
            content = 'BACKGROUND_STARTED 我先继续，结果稍后送达。'
        elif tools and not answered and 'PARALLEL' in text:
            content = '分两个子代理并行调查。'
            calls = [('delegate', {'agent': 'kernel-scout', 'description': '梳理开发流程', 'task': 'CHILD_A trace the development flow'}),
                     ('delegate', {'agent': 'kernel-reviewer', 'description': '核对验证流程', 'task': 'CHILD_B review the verification flow'})]
        elif tools and 'TOOL_ROWS' in text:
            done = sum(1 for m in after_user if m['role'] == 'tool')
            steps = [('read', {'path': 'solution.py'}), ('bash', {'command': 'printf "tick 1\\ntick 2\\n"'}), ('read', {'path': 'missing.py'})]
            if done < len(steps): calls, content = [steps[done]], ''
            else: content = 'TOOL_ROWS_DONE'
        elif answered:
            content = 'PARENT_SYNTHESIS 两个子代理均完成。'
        message = {'role': 'assistant', 'content': content}
        wire = [{'id': f'call-{time.monotonic_ns()}-{i}', 'type': 'function',
                 'function': {'name': name, 'arguments': json.dumps(arguments, ensure_ascii=False)}} for i, (name, arguments) in enumerate(calls)]
        if wire: message['tool_calls'] = wire
        self.send_response(200); self.send_header('Content-Type', 'text/event-stream' if body.get('stream') else 'application/json'); self.end_headers()
        usage = {'prompt_tokens': 100, 'completion_tokens': 10}
        if not body.get('stream'):
            self.wfile.write(json.dumps({'choices': [{'message': message, 'finish_reason': 'tool_calls' if wire else 'stop'}], 'usage': usage}).encode()); return
        chunks = [{'choices': [{'index': 0, 'delta': {'role': 'assistant', 'content': content}}]}]
        if wire: chunks.append({'choices': [{'index': 0, 'delta': {'tool_calls': [dict(call, index=i) for i, call in enumerate(wire)]}}]})
        chunks.append({'choices': [{'index': 0, 'finish_reason': 'tool_calls' if wire else 'stop', 'delta': {}}], 'usage': usage})
        try:
            for chunk in chunks: self.wfile.write(('data: ' + json.dumps(chunk) + '\n\n').encode()); self.wfile.flush()
            self.wfile.write(b'data: [DONE]\n\n')
        except (BrokenPipeError, ConnectionResetError): pass


def overlap(events):
    """True when child 1 was admitted to a model call before child 0's first model call settled."""
    first = {}
    for row in events:
        action = row.get('action_id') or (row.get('action') or {}).get('id', '')
        match = re.search(r'/call/(\d)/leaf/step/0/model/', action)
        if match and row.get('kind') in {'admitted', 'settled'}:
            first.setdefault((match[1], row['kind']), row['seq'])
    return ('1', 'admitted') in first and ('0', 'settled') in first and first[('1', 'admitted')] < first[('0', 'settled')]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--launcher', type=Path, default=ROOT / 'kernelagent')
    args = parser.parse_args()
    output = args.output.resolve(); output.mkdir(parents=True, exist_ok=False)
    project = output / 'project'; project.mkdir()
    (project / 'solution.py').write_text('def add(a, b):\n    return a + b\n')
    server = ThreadingHTTPServer(('127.0.0.1', 0), Provider); threading.Thread(target=server.serve_forever, daemon=True).start()
    config = output / 'config.json'; config.write_text(json.dumps({'providers': {'primary': {'model': 'parallel-fixture',
        'base_url': f'http://127.0.0.1:{server.server_port}/v1', 'api_key_env': 'KERNELBRIDGE_TEST_KEY'}}}))
    home = output / 'home'
    env = {**os.environ, 'TERM': 'xterm-256color', 'KERNELBRIDGE_HOME': str(home), 'KERNELBRIDGE_CONFIG': str(config),
           'KERNELBRIDGE_SESSIONS': str(Path('/tmp').resolve() / ('kb-par-' + uuid.uuid4().hex[:8])), 'KERNELBRIDGE_TEST_KEY': 'fixture'}
    ui = Terminal([str(args.launcher.resolve())], project, env)
    checks, client = [], None
    def settle(seconds=.4):
        end = time.monotonic() + seconds
        while time.monotonic() < end: ui.pump()
    try:
        ui.until(lambda: '/help' in ui.text())
        binding = json.loads(next(home.glob('runs/*/application.json')).read_text())
        client = Client(Path(binding['host_directory']))
        ui.send('启动两个子代理 PARALLEL')
        ui.until(lambda: '等待 2 个子代理完成' in ui.text() and ui.text().count('⎿') >= 2, timeout=60)
        (output / 'running-screen.txt').write_text(ui.text())
        assert 'kernel-scout(梳理开发流程)' in ui.text() and 'kernel-reviewer(核对验证流程)' in ui.text()
        assert '子代理 2 个运行中' in ui.text()
        checks.append('two_children_visible_running_together')
        ui.until(lambda: 'PARENT_SYNTHESIS' in ui.text() and '等待输入' in ui.text(), timeout=90)
        settle(1.5)
        screen = ui.text(); (output / 'settled-screen.txt').write_text(screen)
        assert screen.count('完成 · 读取 3 · 模型 4') == 2, screen
        assert 'durable host receipt' not in screen and '子代理 2 个运行中' not in screen and '子代理 1 个运行中' not in screen
        journal = next(home.glob('runs/*/state/events.jsonl'))
        events = [json.loads(line) for line in journal.read_text().splitlines()]
        assert overlap(events), 'children did not overlap in the Journal'
        state = client.call('status')
        assert [child['status'] for child in state['subagents']] == ['completed', 'completed']
        assert not state['pending_effect_ids'] and state['budget']['reserved'] == 0
        checks.append('children_overlap_in_journal_and_settle_in_reply_order')
        os.write(ui.master, b'\x0f'); ui.until(lambda: 'CHILD_A 结论' in ui.text() and 'CHILD_B 结论' in ui.text())
        assert '{"outcome"' not in ui.text() and '建议性结论' in ui.text()
        (output / 'expanded-screen.txt').write_text(ui.text())
        os.write(ui.master, b'\x0f'); settle()
        checks.append('ctrl_o_shows_reports_not_receipt_json')
        ui.send('/agents'); ui.until(lambda: '本会话（2）' in ui.text() and '查找文件、搜索代码' in ui.text())
        assert '每次回复最多' not in ui.text() and '可以直接说' not in ui.text()
        for _ in range(2): os.write(ui.master, b'\x1b[B')
        os.write(ui.master, b'\r'); ui.until(lambda: '任务：CHILD_' in ui.text() and '结论' in ui.text())
        os.write(ui.master, b'\x1b'); ui.until(lambda: '本会话（2）' in ui.text())
        os.write(ui.master, b'\x1b'); ui.until(lambda: '本会话（2）' not in ui.text())
        checks.append('agents_panel_lists_and_opens_reports')
        ui.send('TOOL_ROWS'); ui.until(lambda: 'TOOL_ROWS_DONE' in ui.text() and '等待输入' in ui.text(), timeout=60)
        settle()
        screen = ui.text(); (output / 'tool-rows-screen.txt').write_text(screen)
        assert '⏺ Read(solution.py)' in screen and '读取 2 行' in screen, screen
        assert '⏺ Bash(printf' in screen and '退出码 0' in screen
        error = next(line for line in screen.splitlines() if '失败：ENOENT' in line)
        assert str(project) not in error and "'missing.py'" in error
        checks.append('tool_rows_status_header_summary_and_relative_errors')
        ui.send('BACKGROUND_START')
        ui.until(lambda: 'BACKGROUND_STARTED' in ui.text() and '后台运行' in ui.text() and '等待输入' in ui.text(), timeout=60)
        (output / 'background-running-screen.txt').write_text(ui.text())
        assert '子代理 1 个运行中' in ui.text()
        ui.until(lambda: 'BACKGROUND_SEEN' in ui.text() and '等待输入' in ui.text(), timeout=90)
        settle(1)
        screen = ui.text(); (output / 'background-done-screen.txt').write_text(screen)
        assert '⏺ kernel-scout「后台扫描」已完成' in screen, screen
        state = client.call('status')
        background = state['subagents'][-1]
        assert background['background'] and background['status'] == 'completed' and 'CHILD_BG' in background['summary']
        assert not state['pending_effect_ids'] and state['budget']['reserved'] == 0
        assert any(item['initiator'] == 'agent' and item['text'].startswith('[子代理结果]') and item['status'] == 'completed'
                   for item in state['inputs'].values())
        checks.append('background_child_notifies_and_parent_continues')
        ui.send('/quit'); ui.until(lambda: ui.child.poll() is not None); assert ui.child.returncode == 0
        (output / 'scenario.json').write_text(json.dumps({'status': 'passed', 'checks': checks}, ensure_ascii=False, indent=2) + '\n')
        print(json.dumps({'status': 'passed', 'checks': checks}, ensure_ascii=False), flush=True)
    finally:
        (output / 'last-screen.txt').write_text(ui.text())
        try: ui.close(output / 'transcript.txt')
        except Exception: pass
        if client: client.close()
        cleanup_fixture_hosts(home / 'runs') if (home / 'runs').exists() else None
        server.shutdown()


if __name__ == '__main__': main()
