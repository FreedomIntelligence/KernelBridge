"""Real Pi: settled blocker review, isolated noncompletion and user pause.

The local scripted provider tests wire delivery and scheduling, not reasoning
quality or device acceptance. It never executes a model or an NPU job.
"""
import argparse
from collections import Counter
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import threading
import uuid

from codegen_edges import Terminal
from product_scenario import ROOT, cleanup_fixture_hosts, prepare
from pty_scenario import Client


PROJECT_INSTRUCTIONS = 'PROJECT_INSTRUCTIONS_ALREADY_AUTHORIZE_READ: read solution.py within this project.\n'


class BlockerProvider(BaseHTTPRequestHandler):
    requests = []
    steps = Counter()
    pause_review = threading.Event()
    release_review = threading.Event()

    def log_message(self, *_):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.requests.append(body)
        raw = json.dumps(body, ensure_ascii=False)
        system = body['messages'][0]['content']
        # The most recent objective wins when two Goals share a project session.
        marker = next(name for name in ('PAUSE', 'ISOLATED', 'PROJECT') if 'BLOCKER_' + name in raw)
        tools = {tool['function']['name'] for tool in body.get('tools', [])}
        calls = []
        if system.startswith('Assess only whether the working agent'):
            assert not tools and len(body['messages']) == 2
            facts = json.loads(body['messages'][1]['content'])
            assert facts['purpose'] == 'blocker' and facts['proposal']['action_id']
            assert facts['workspace_authority'] and facts['frozen_task_records']
            if marker in {'PROJECT', 'PAUSE'}:
                instruction = next(item for item in facts['frozen_instruction_records'] if item['path'] == 'AGENTS.md')
                assert instruction['text'] == PROJECT_INSTRUCTIONS, 'frozen project instructions differ in actual reviewer request'
                assert instruction['sha256'] == hashlib.sha256(PROJECT_INSTRUCTIONS.encode()).hexdigest()
            if marker == 'PAUSE':
                self.pause_review.set()
                assert self.release_review.wait(30), 'fixture pause was not delivered'
            content = json.dumps({'status': 'continue', 'summary': 'READ_REVIEW_FEEDBACK: inspect the already authorized solution.py before claiming a blocker'})
        elif system.startswith('You independently assess a coding Goal'):
            assert not tools and len(body['messages']) == 2
            content = json.dumps({'status': 'met', 'summary': 'The retained local read meets this fixture Goal'})
        else:
            step = self.steps[marker]
            self.steps[marker] += 1
            content = 'BLOCKER_FIXTURE'
            if step == 0 or marker == 'ISOLATED':
                calls = [('update_goal', {'status': 'blocked', 'summary': 'Claimed fixture blocker without checking the authorized file'})]
            elif marker == 'PROJECT' and step == 1:
                assert 'READ_REVIEW_FEEDBACK' in raw, 'review feedback did not reach the next actual request'
                calls = [('read', {'path': 'solution.py'})]
            elif marker == 'PROJECT' and step == 2:
                calls = [('update_goal', {'status': 'complete', 'summary': 'Read solution.py through the original tool'})]
            elif marker == 'PROJECT' and step == 3:
                # Existing completion review runs after the normal final answer
                # settles the input, rather than inside its update_goal tool.
                content = 'Read completed; the original receipt is retained.'
            else:
                raise AssertionError(('unexpected parent call', marker, step))
            assert all(name in tools for name, _ in calls), (calls, tools)
        wire = [{'id': f'blocker-{len(self.requests)}-{i}', 'type': 'function',
                 'function': {'name': name, 'arguments': json.dumps(arguments)}}
                for i, (name, arguments) in enumerate(calls)]
        message = {'role': 'assistant', 'content': content}
        if wire:
            message['tool_calls'] = wire
        finish = 'tool_calls' if wire else 'stop'
        usage = {'prompt_tokens': 100, 'completion_tokens': 10}
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream' if body.get('stream') else 'application/json')
        self.end_headers()
        try:
            if not body.get('stream'):
                self.wfile.write(json.dumps({'choices': [{'message': message, 'finish_reason': finish}], 'usage': usage}).encode())
                return
            chunks = [{'choices': [{'index': 0, 'delta': {'role': 'assistant', 'content': content}}]}]
            if wire:
                chunks.append({'choices': [{'index': 0, 'delta': {'tool_calls': [dict(call, index=i) for i, call in enumerate(wire)]}}]})
            chunks.append({'choices': [{'index': 0, 'delta': {}, 'finish_reason': finish}], 'usage': usage})
            for chunk in chunks:
                self.wfile.write(('data: ' + json.dumps(chunk) + '\n\n').encode())
                self.wfile.flush()
            self.wfile.write(b'data: [DONE]\n\n')
        except (BrokenPipeError, ConnectionResetError):
            pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--deployment', type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    project = output / 'project'
    project.mkdir()
    (project / 'solution.py').write_text('x = 1\n')
    (project / 'AGENTS.md').write_text(PROJECT_INSTRUCTIONS)
    server = ThreadingHTTPServer(('127.0.0.1', 0), BlockerProvider)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    endpoint = f'http://127.0.0.1:{server.server_port}/v1'
    config = output / 'config.json'
    config.write_text(json.dumps({'providers': {'primary': {'model': 'blocker-fixture', 'base_url': endpoint,
        'api_key_env': 'KERNELBRIDGE_TEST_KEY', 'max_retries': 0}}, 'goal_review': {'enabled': True}}))
    env = {**os.environ, 'TERM': 'xterm-256color', 'PI_OFFLINE': '1', 'KERNELBRIDGE_TEST_KEY': 'fixture-only',
           'KERNELBRIDGE_HOME': str(output / 'home'),
           'KERNELBRIDGE_SESSIONS': str(Path('/tmp').resolve() / ('kb-blocker-' + uuid.uuid4().hex[:8]))}
    ui = None
    clients = []
    checks = []
    states = {}
    isolated = output / 'isolated'
    try:
        ui = Terminal([str(ROOT / 'kernelagent'), '--config', str(config)], project, env)
        ui.until(lambda: '等待输入' in ui.text())
        binding = json.loads(next((output / 'home/runs').glob('*/application.json')).read_text())
        client = Client(Path(binding['host_directory']))
        clients.append(client)
        assert not BlockerProvider.requests
        checks.append('ordinary_idle_no_blocker_review')
        ui.send('/goal BLOCKER_PROJECT read solution.py')
        ui.until(lambda: (client.call('status')['goal'] or {}).get('status') == 'completed')
        state = client.call('status')
        states['project'] = state
        assert state['goal']['completion_kind'] == 'review'
        assert len([i for i in state['inputs'].values() if i.get('goal_blocker_review')]) == 1
        assert state['goal_usage']['model_turns'] == len(BlockerProvider.requests) == 6
        assert not state['pending_effect_ids'] and not state['trials']
        checks.extend(['settled_blocker_continue_feedback_reaches_actual_pi_wire', 'continue_once_then_normal_completion_review',
                       'shared_review_model_call_ledger'])
        before = BlockerProvider.steps['PAUSE']
        ui.send('/goal BLOCKER_PAUSE read solution.py')
        ui.until(BlockerProvider.pause_review.is_set)
        client.call('control', {'input_id': str(uuid.uuid4()), 'operation': 'pause', 'channel': 'sdk'})
        BlockerProvider.release_review.set()
        ui.until(lambda: client.call('status')['goal']['status'] == 'paused' and not client.call('status')['pending_effect_ids'])
        assert BlockerProvider.steps['PAUSE'] == before + 1
        states['paused'] = client.call('status')
        assert not any(i.get('goal_blocker_review') and i.get('goal_id') == states['paused']['goal']['goal_id']
                       for i in states['paused']['inputs'].values())
        checks.append('user_pause_wins_over_late_blocker_verdict')
        ui.send('/quit')
        ui.until(lambda: ui.child.poll() is not None)
        assert ui.child.returncode == 0
        ui.close(output / 'project-transcript.txt')
        ui = None

        options = prepare(isolated, args.deployment.resolve(), endpoint, name='blocker-isolated')
        path = isolated / 'config.json'
        isolated_config = json.loads(path.read_text())
        isolated_config['goal_review'] = {'enabled': True}
        path.write_text(json.dumps(isolated_config))
        path = isolated / 'task.json'
        task = json.loads(path.read_text())
        task['goal'] = 'BLOCKER_ISOLATED read solution.py; this fixture has no device acceptance'
        path.write_text(json.dumps(task))
        ui = Terminal([str(ROOT / 'kernelagent'), 'tui', '--task', options['task'], '--config', options['config'],
                       '--output', str(isolated), '--python', sys.executable, '--expected-python-modules',
                       options['expectedPythonModules'], '--session-only', '--goal-from-task'], project, env)
        ui.until(lambda: (isolated / 'application.json').exists())
        binding = json.loads((isolated / 'application.json').read_text())
        directory = Path(binding['host_directory'])
        ui.until(lambda: (directory / 'host.sock').exists())
        client = Client(directory)
        clients.append(client)
        ui.until(lambda: (client.call('status')['goal'] or {}).get('status') == 'awaiting_user')
        state = client.call('status')
        states['isolated'] = state
        assert state['goal']['reason'] == 'blocker_no_new_information', state['goal']
        assert state['goal_usage']['model_turns'] == 3 and BlockerProvider.steps['ISOLATED'] == 2
        assert len([i for i in state['inputs'].values() if i.get('goal_blocker_review')]) == 1
        assert not state['pending_effect_ids'] and not state['evidence']
        checks.extend(['isolated_blocker_review_without_completion_authority', 'repeat_blocker_without_information_stops_once'])
        ui.send('/quit')
        ui.until(lambda: ui.child.poll() is not None)
        assert ui.child.returncode == 0
        result = {'status': 'passed', 'checks': checks, 'requests': len(BlockerProvider.requests),
                  'scope': 'scripted local provider through real Pi PTY; no model reasoning or NPU qualification'}
        (output / 'scenario.json').write_text(json.dumps(result, indent=2) + '\n')
        print(json.dumps(result))
    finally:
        BlockerProvider.release_review.set()
        (output / 'model-requests.json').write_text(json.dumps(BlockerProvider.requests, ensure_ascii=False))
        (output / 'states.json').write_text(json.dumps(states, ensure_ascii=False))
        if ui:
            ui.close(output / 'transcript.txt')
        for client in clients:
            client.close()
        for run in (output / 'home/runs').glob('*'):
            cleanup_fixture_hosts(run)
        cleanup_fixture_hosts(isolated)
        server.shutdown()


if __name__ == '__main__':
    main()
