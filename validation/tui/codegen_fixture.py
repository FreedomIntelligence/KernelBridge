"""Deterministic codegen provider: real Pi tools, no external API or device."""
from collections import defaultdict
from http.server import BaseHTTPRequestHandler
import json
import time


class CodegenProvider(BaseHTTPRequestHandler):
    requests = []
    turns = defaultdict(int)

    def log_message(self, *_):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.requests.append(body)
        model = body['model']
        turn = self.turns[model]
        self.turns[model] += 1
        latest = next((item.get('content', '') for item in reversed(body['messages']) if item['role'] == 'user'), '')
        text = json.dumps(latest, ensure_ascii=False)
        tools = {item['function']['name'] for item in body.get('tools', [])}
        calls = [('read', {'path': 'solution.py'}),
                 ('edit', {'path': 'solution.py', 'edits': [{'oldText': 'x = 1', 'newText': 'x = 2'}]}),
                 ('bash', {'command': 'test "$(cat solution.py)" = "x = 2" && printf "checked\\n" > shell-check.txt'})]
        call = calls[turn] if turn < len(calls) else None
        if 'finish_goal' in tools:
            call = ('finish_goal', {'summary': 'Local codegen fixture complete; file and shell checks passed. No device result.'})
        content = f'CODEGEN_DONE_{turn} 中文'
        if 'SLOW_INPUT' in text:
            time.sleep(1)
        if 'FAIL_ONCE' in text and not any('FAIL_ONCE' in json.dumps(item, ensure_ascii=False) for item in self.requests[:-1]):
            self.send_response(503); self.end_headers(); return
        if 'FAIL_ALWAYS' in text and json.dumps(self.requests[-1]['messages'][-1], ensure_ascii=False).find('FAIL_ALWAYS') >= 0:
            self.send_response(503); self.end_headers(); return
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.end_headers()
        chunks = [{'choices': [{'index': 0, 'delta': {'role': 'assistant', 'content': content}}]}]
        if call:
            chunks.append({'choices': [{'index': 0, 'delta': {'tool_calls': [{'index': 0, 'id': f'fixture-{turn}', 'type': 'function',
                'function': {'name': call[0], 'arguments': json.dumps(call[1])}}]}}]})
        chunks.append({'choices': [{'index': 0, 'finish_reason': 'tool_calls' if call else 'stop', 'delta': {}}],
                       'usage': {'prompt_tokens': 100, 'completion_tokens': 10}})
        try:
            for chunk in chunks:
                self.wfile.write(('data: ' + json.dumps(chunk) + '\n\n').encode()); self.wfile.flush()
            self.wfile.write(b'data: [DONE]\n\n')
        except (BrokenPipeError, ConnectionResetError):
            pass
