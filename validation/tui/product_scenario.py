"""Real product host/SDK with a local scripted SSE provider and local evaluator.

Usage: python product_scenario.py --deployment FROZEN_DEPLOYMENT --output NEW_DIR
No external inference, NPU job or platform submission is performed.
"""
import argparse
from collections import defaultdict
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[2]


class Provider(BaseHTTPRequestHandler):
    turns = defaultdict(int)
    requests = []
    def log_message(self, *_): pass
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.requests.append(body)
        model = body['model']
        index = self.turns[model]
        self.turns[model] += 1
        if model == 'transport' and index == 0:
            self.send_response(503)
            self.end_headers()
            return
        if model == 'transport': index -= 1
        if model == 'steering':
            time.sleep(1)
        calls = [('read', {'path': 'solution.py'}),
                 ('edit', {'path': 'solution.py', 'edits': [{'oldText': 'x = 1', 'newText': 'x = 2'}]}),
                 ('evaluate_candidate', {})]
        call = calls[index] if index < len(calls) else None
        content = f'fixture turn {index}: 中文 '
        if model == 'tui' and index == 4:
            content += 'long output for compaction and terminal scrolling. ' * 2500
        if model == 'long' and index == 20:
            content += 'persistent context fixture. ' * 3500
        choice = {'index': 0, 'finish_reason': 'tool_calls' if call else 'stop'}
        if body.get('stream'):
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()
            chunks = [{'choices': [{'index': 0, 'delta': {'role': 'assistant', 'content': content[:15]}}]},
                      {'choices': [{'index': 0, 'delta': {'content': content[15:]}}]}]
            if call:
                chunks.append({'choices': [{'index': 0, 'delta': {'tool_calls': [{'index': 0, 'id': 'provider-call',
                    'type': 'function', 'function': {'name': call[0], 'arguments': json.dumps(call[1])}}]}}]})
            chunks.append({'choices': [{**choice, 'delta': {}}], 'usage': {'prompt_tokens': 100, 'completion_tokens': 10}})
            for chunk in chunks:
                self.wfile.write(('data: ' + json.dumps(chunk) + '\n\n').encode())
                self.wfile.flush()
            self.wfile.write(b'data: [DONE]\n\n')
        else:
            message = {'role': 'assistant', 'content': content}
            if call: message['tool_calls'] = [{'id': 'provider-call', 'type': 'function', 'function': {'name': call[0], 'arguments': json.dumps(call[1])}}]
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'choices': [{**choice, 'message': message}], 'usage': {'prompt_tokens': 100, 'completion_tokens': 10}}).encode())


def prepare(output, deployment, endpoint, name='sdk'):
    output.mkdir(mode=0o700, parents=True)
    expected = deployment / 'expected.json'
    evaluator = output / 'fixture-evaluator.py'
    shutil.copyfile(ROOT / 'validation/tui/evaluator.py', evaluator)
    schema = {'type': 'object', 'required': ['status', 'data'], 'properties': {
        'status': {'const': 'completed'}, 'data': {'type': 'object', 'required': ['correct', 'candidate_sha256'],
            'properties': {'correct': {'const': True}, 'candidate_sha256': {'type': 'string'}}}}}
    config = {'python_modules': json.loads(expected.read_text()), 'state_dir': str(output / 'state'),
        'workspace': str(output / 'workspace'), 'credits': 200, 'writable_paths': ['solution.py'], 'readonly_paths': ['reference'],
        'policy': {'max_steps': 40, 'context_bytes': 524288},
        'providers': {'primary': {'model': name, 'base_url': endpoint, 'api_key_env': 'KERNELBRIDGE_TEST_KEY',
            'allow_insecure_http': True, 'wire_options': {'native_turns': True, 'tool_choice': 'auto'}}},
        'business_tools': [{'name': 'evaluate_candidate', 'description': 'Evaluate the local fixture. No NPU performance claim.',
            'argv': [sys.executable, str(evaluator)], 'parameters': {'type': 'object', 'properties': {}, 'additionalProperties': False},
            'capture_paths': ['solution.py'], 'credits': 2, 'files': [{'path': str(evaluator), 'sha256': hashlib.sha256(evaluator.read_bytes()).hexdigest()}]}],
        'candidate_retention': {'tool': 'evaluate_candidate', 'source_hashes': {'solution.py': 'data.candidate_sha256'}, 'outcome_schema': schema},
        'pi_worker': {'node': shutil.which('node'), 'root': str(ROOT / 'source/apps/agent'), 'timeout': 120}}
    if name == 'recovery':
        config['business_tools'][0]['argv'].append('--gate=' + str(output / 'evaluation-accepted'))
    task = {'id': 'local-' + name, 'scope': 'test', 'goal': 'Read, edit x = 1 to x = 2, and evaluate the local fixture.',
        'contract': {'acceptance': {'evidence_tier': 'E1'}}, 'seed_files': {'solution.py': 'x = 1\n', 'reference/spec.txt': 'immutable'}}
    for path, value in [('task.json', task), ('config.json', config)]:
        (output / path).write_text(json.dumps(value))
    options = {'task': str(output / 'task.json'), 'config': str(output / 'config.json'), 'output': str(output),
        'python': sys.executable, 'expectedPythonModules': str(expected), 'sessionOnly': True}
    (output / 'options.json').write_text(json.dumps(options))
    return options


def cleanup_fixture_hosts(output):
    """Reap only hosts created inside this new fixture output after a child fails."""
    for path in [output / 'application.json', *output.glob('*/application.json')]:
        try:
            binding = json.loads(path.read_text())
            directory = Path(binding['host_directory'])
            with socket.socket(socket.AF_UNIX) as connection:
                connection.settimeout(1)
                connection.connect(str(directory / 'host.sock'))
                with connection.makefile('rwb', buffering=0) as stream:
                    stream.write((json.dumps({'id': 1, 'method': 'authenticate', 'params': {'token': (directory / 'token').read_text()}}) + '\n').encode())
                    if 'error' in json.loads(stream.readline()): continue
                    stream.write(b'{"id":2,"method":"status","params":{}}\n')
                    state = json.loads(stream.readline())['result']
                    if state['application_session_id'] == binding['application_session_id'] and Path(state['output']) == path.parent:
                        os.kill(state['host_pid'], signal.SIGTERM)
        except (OSError, ValueError, KeyError):
            pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--deployment', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--scenario', choices=['sdk', 'long', 'recovery', 'lifecycle', 'steering', 'transport', 'admission', 'fork'], default='sdk')
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists(): raise ValueError('preserve the original validation output; choose a new directory')
    provider = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
    threading.Thread(target=provider.serve_forever, daemon=True).start()
    options = prepare(output, args.deployment.resolve(), f'http://127.0.0.1:{provider.server_port}/v1', args.scenario)
    registry = Path('/tmp').resolve() / ('kb-reg-' + hashlib.sha256(str(output).encode()).hexdigest()[:10])
    environment = {**os.environ, 'KERNELBRIDGE_TEST_KEY': 'fixture-key-not-a-real-credential',
        'PYTHONPATH': str(args.deployment.resolve() / 'unpacked'), 'KERNELBRIDGE_SESSIONS': str(registry)}
    try:
        result = subprocess.run([shutil.which('node'), str(ROOT / ('validation/tui/' + args.scenario + '-scenario.mjs')), str(output / 'options.json')],
            env=environment, text=True, capture_output=True, timeout=240)
        (output / 'sdk.stdout').write_text(result.stdout)
        (output / 'sdk.stderr').write_text(result.stderr)
        (output / 'model-requests.json').write_text(json.dumps(Provider.requests, indent=2))
        print(result.stdout, end='')
        print(result.stderr, file=sys.stderr, end='')
        return result.returncode
    finally:
        cleanup_fixture_hosts(output)
        provider.shutdown()


if __name__ == '__main__': raise SystemExit(main())
