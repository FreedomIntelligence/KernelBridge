"""Real Pi PTY: Goal, community subagents, fork, compact and cross-host resume."""
import argparse
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import importlib.util
import os
from pathlib import Path
import re
import sys
import threading
import time
import uuid

from codegen_edges import Terminal
from product_scenario import ROOT, cleanup_fixture_hosts
from pty_scenario import Client


class Provider(BaseHTTPRequestHandler):
    requests = []
    children = defaultdict(int)
    pipeline = []
    pipeline_index = 0
    def log_message(self, *_): pass
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.requests.append(body)
        tools = {tool['function']['name'] for tool in body.get('tools', [])}
        latest = next((m.get('content', '') for m in reversed(body['messages']) if m['role'] == 'user'), '')
        text = json.dumps(latest, ensure_ascii=False)
        raw = json.dumps(body, ensure_ascii=False)
        child = 'read_file' in tools and 'read' not in tools
        calls = []; content = 'ADVANCED_DONE 中文'
        if child:
            marker = re.search('CHILD_[A-Z0-9_]+', text)
            label = marker[0] if marker else 'CHILD_DEFAULT'
            count = self.children[label]; self.children[label] += 1
            if label == 'CHILD_FAIL': self.send_response(503); self.end_headers(); return
            if label == 'CHILD_SLOW': time.sleep(2)
            if count == 0: calls = [('read_file', {'path': 'solution.py'})]
            else: content = 'CHILD_READ_COMPLETE x = 1'
        elif not tools:
            content = 'Compressed: preserve ADVANCED_CONSTRAINT, solution.py and original receipt handles. 中文'
        else:
            # Only act once for the latest user input; native tool-result pairing
            # must be visible on the parent's next model call.
            after_user = []
            for message in reversed(body['messages']):
                if message['role'] == 'user': break
                after_user.append(message)
            has_results = any(m['role'] == 'tool' for m in after_user)
            if 'RUN_PIPELINE' in text and self.pipeline_index < len(self.pipeline):
                calls = [self.pipeline[self.pipeline_index]]; type(self).pipeline_index += 1
            elif not has_results:
                if 'DELEGATE_TWO' in text:
                    calls = [('delegate', {'agent': 'kernel-scout', 'task': 'CHILD_A read solution.py', 'mode': 'read_only'}),
                             ('delegate', {'agent': 'kernel-reviewer', 'task': 'CHILD_B read solution.py', 'mode': 'read_only'}),
                             ('delegate', {'agent': 'kernel-scout', 'task': 'CHILD_LIMIT should not start', 'mode': 'read_only'})]
                elif 'DELEGATE_FAIL' in text:
                    calls = [('delegate', {'agent': 'kernel-scout', 'task': 'CHILD_FAIL', 'mode': 'read_only'})]
                elif 'DELEGATE_SLOW' in text:
                    calls = [('delegate', {'agent': 'kernel-scout', 'task': 'CHILD_SLOW', 'mode': 'read_only'})]
                elif 'BLOCK_ME' in text and 'update_goal' in tools:
                    calls = [('update_goal', {'status': 'blocked', 'summary': 'Need the fixture confirmation'})]
                elif 'UNBLOCK_COMPLETE' in text and 'update_goal' in tools:
                    calls = [('update_goal', {'status': 'complete', 'summary': 'Fixture confirmed; original local checks retained'})]
                elif 'LONG_CONTEXT' in text: content = 'ADVANCED_CONSTRAINT ' + ('original source and receipts retained. ' * 3000)
                elif 'READ_STEP' in text: calls = [('read', {'path': 'solution.py'})]
                elif 'FORK_ERRORS' in text: calls = [('read', {'path': 'missing-fixture-' + str(i)}) for i in range(3)]
        message = {'role': 'assistant', 'content': content}
        wire = [{'id': 'call-' + str(len(self.requests)) + '-' + str(i), 'type': 'function',
                 'function': {'name': name, 'arguments': json.dumps(arguments)}} for i, (name, arguments) in enumerate(calls)]
        if wire: message['tool_calls'] = wire
        self.send_response(200); self.send_header('Content-Type', 'text/event-stream' if body.get('stream') else 'application/json'); self.end_headers()
        if not body.get('stream'):
            self.wfile.write(json.dumps({'choices': [{'message': message, 'finish_reason': 'tool_calls' if wire else 'stop'}], 'usage': {'prompt_tokens': 100, 'completion_tokens': 10}}).encode()); return
        chunks = [{'choices': [{'index': 0, 'delta': {'role': 'assistant', 'content': content}}]}]
        if wire: chunks.append({'choices': [{'index': 0, 'delta': {'tool_calls': [dict(call, index=i) for i, call in enumerate(wire)]}}]})
        chunks.append({'choices': [{'index': 0, 'finish_reason': 'tool_calls' if wire else 'stop', 'delta': {}}], 'usage': {'prompt_tokens': 100, 'completion_tokens': 10}})
        try:
            for chunk in chunks: self.wfile.write(('data: ' + json.dumps(chunk) + '\n\n').encode()); self.wfile.flush()
            self.wfile.write(b'data: [DONE]\n\n')
        except (BrokenPipeError, ConnectionResetError): pass


def main():
    parser = argparse.ArgumentParser(); parser.add_argument('--output', required=True, type=Path); parser.add_argument('--launcher', type=Path, default=ROOT / 'kernelagent')
    args = parser.parse_args(); output = args.output.resolve(); output.mkdir(parents=True, exist_ok=False)
    project = output / 'project'; project.mkdir(); (project / 'solution.py').write_text('x = 1\n')
    (project / 'AGENTS.md').write_text('Preserve ADVANCED_CONSTRAINT. Delegate only if explicitly requested.\n')
    server = ThreadingHTTPServer(('127.0.0.1', 0), Provider); threading.Thread(target=server.serve_forever, daemon=True).start()
    sys.path.insert(0, str(ROOT / 'examples/scatter'))
    spec = importlib.util.spec_from_file_location('scatter_campaign', ROOT / 'examples/scatter/prepare.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    module.prepare(project / 'scatter', sys.executable)
    pipeline = json.loads((project / 'scatter/pipeline-profile.json').read_text())
    Provider.pipeline = [
        ('pipeline_bind', {'contract': json.loads((project / 'scatter/contract.json').read_text())}),
        ('pipeline_plan', {'stage': 'B', 'record': {'families': 'sparse/collision/wide'}}),
        ('pipeline_plan', {'stage': 'C', 'record': {'hypothesis': 'scatter each source row once'}}),
        ('pipeline_freeze', {'files': {'native.cpp': str(project / 'scatter/native.cpp')}, 'origin': {'scope': 'local fixture'}}),
        ('pipeline_run', {'stage': 'F', 'driver': 'check'}),
        ('pipeline_pair', {'driver': 'measure'}),
        ('pipeline_promote', {}),
        ('pipeline_run', {'stage': 'J', 'driver': 'ir'}),
        ('pipeline_run', {'stage': 'J', 'driver': 'profiler'}),
        ('pipeline_archive', {'knowledge': {'mechanism': 'row scatter', 'limitations': 'CPU only'}}),
        ('pipeline_status', {}),
    ]
    config = output / 'config.json'; config.write_text(json.dumps({'providers': {'primary': {'model': 'advanced',
        'base_url': f'http://127.0.0.1:{server.server_port}/v1', 'api_key_env': 'KERNELBRIDGE_TEST_KEY'}},
        # This scenario checks count limits and recovery, not wall-clock
        # deadlines (covered by dedicated cancellation tests). Allow loaded CI
        # machines to finish the two actual provider/tool exchanges.
        'subagents': {'max_per_turn': 2, 'max_total': 6, 'timeout': 120}, **pipeline}))
    home = output / 'home'; registry = Path('/tmp').resolve() / ('kb-advanced-' + uuid.uuid4().hex[:8])
    env = {**os.environ, 'TERM': 'xterm-256color', 'KERNELBRIDGE_HOME': str(home), 'KERNELBRIDGE_SESSIONS': str(registry),
           'KERNELBRIDGE_CONFIG': str(config), 'KERNELBRIDGE_TEST_KEY': 'local-protocol-fixture'}
    ui = None; clients = []; checks = []
    def launch(arguments=()):
        term = Terminal([str(args.launcher.resolve()), *arguments], project, env)
        term.until(lambda: '等待输入' in term.text() or 'ADVANCED_DONE' in term.text()); return term
    def connect_latest():
        files = sorted(home.glob('runs/*/application.json'), key=lambda p: p.stat().st_mtime)
        binding = json.loads(files[-1].read_text()); client = Client(Path(binding['host_directory'])); client.socket.settimeout(120); clients.append(client); return client
    def status(): return client.call('status')
    def send(text):
        before = set(status()['inputs']); ui.send(text)
        ui.until(lambda: any(k not in before and v['text'] == text and v['status'] in {'completed', 'failed', 'interrupted'} for k, v in status()['inputs'].items()))
        ui.until(lambda: not status()['calls'] and not status()['pending_effect_ids'])
        receipt = next(v for k, v in status()['inputs'].items() if k not in before and v['text'] == text)
        assert receipt['status'] == 'completed', (receipt, ui.text())
        end = time.monotonic() + .25
        while time.monotonic() < end: ui.pump()
    def finish(label):
        ui.send('/quit'); ui.until(lambda: ui.child.poll() is not None); assert ui.child.returncode == 0
        ui.close(output / (label + '.txt'))
    try:
        ui = launch(); client = connect_latest(); first_id = status()['application_session_id']
        send('READ_STEP'); assert not status()['subagents']; checks.append('ordinary_does_not_spawn')
        send('RUN_PIPELINE for the local scatter operators')
        workflow_events = list(home.glob('runs/*/inputs/operator-pipeline/event-*.json'))
        assert workflow_events, 'pipeline not reached through harness'
        workflow = json.loads(sorted(workflow_events)[-1].read_text())['body']
        assert all(row['status'] == 'completed' for row in workflow['receipts']), workflow['receipts']
        assert workflow['incumbent'] and workflow['check']['scope'] == 'local_cpu'
        checks.append('model_calls_registered_pipeline_scatter_checks_and_diagnostics')
        send('Please use subagents: DELEGATE_TWO')
        children = status()['subagents']; assert len(children) == 3, children
        assert [r['status'] for r in children] == ['completed', 'completed', 'failed'], children
        assert Provider.children['CHILD_LIMIT'] == 0 and Provider.children['CHILD_A'] == 2 and Provider.children['CHILD_B'] == 2, Provider.children
        assert status()['budget']['limit'] is None
        checks.append('upstream_two_readonly_children_and_count_limit')
        send('Please use subagent: DELEGATE_FAIL')
        assert status()['subagents'][-1]['status'] == 'failed'; send('PARENT_AFTER_FAILURE')
        checks.append('child_provider_failure_settled_parent_continues')
        branch = status()['checkpoint']['pi_session_id']
        send('FORK_ERRORS')
        assert any(item.get('operation') == 'fork_suggestion' for item in status()['configurations'])
        assert status()['checkpoint']['pi_session_id'] == branch
        checks.append('manual_fork_hint_without_automatic_branch')
        ui.send('/subagents'); ui.until(lambda: 'kernel-scout' in ui.text() and '本会话（4）' in ui.text() and '超出数量上限' in ui.text())
        os.write(ui.master, b'\x1b'); ui.until(lambda: '本会话（4）' not in ui.text())
        send('/name advanced first')
        send('LONG_CONTEXT'); before = len(Provider.requests); send('/compact')
        compact_calls = Provider.requests[before:]
        assert 1 <= len(compact_calls) <= 2 and all(not call.get('tools') for call in compact_calls), compact_calls
        send('AFTER_COMPACT')
        assert 'ADVANCED_CONSTRAINT' in json.dumps(Provider.requests[-1])
        assert (project / 'solution.py').read_text() == 'x = 1\n'
        checks.append('semantic_compaction_preserves_constraints_and_source')
        send('/new')
        assert status()['budget']['limit'] is None and not status()['trials']
        before_empty = len(Provider.requests); send('/fork')
        assert len(Provider.requests) == before_empty
        send('NEW_CONTEXT_AFTER_COMPACTION')
        previous = status()['checkpoint']['pi_session_id']; spent = status()['budget']['spent']
        send('/fork'); assert status()['checkpoint']['pi_session_id'] != previous
        assert status()['budget']['spent'] == spent and not status()['trials']
        send('FORK_BRANCH_MARKER'); checks.append('ordinary_fork_preserves_files_ledger_and_branch')
        send('/goal NO_TOOL_PROGRESS')
        ui.until(lambda: status()['goal']['status'] == 'awaiting_user')
        assert status()['goal']['reason'] == 'no_tool_progress'
        before = len(Provider.requests); send('/goal resume'); ui.until(lambda: status()['goal']['status'] == 'awaiting_user')
        assert len(Provider.requests) == before + 1
        checks.append('no_tool_goal_stops_and_explicit_resume_rearms')
        send('/goal BLOCK_ME'); ui.until(lambda: status()['goal']['status'] == 'awaiting_user')
        assert status()['goal']['reason'] == 'blocked'
        send('UNBLOCK_COMPLETE'); ui.until(lambda: status()['goal']['status'] == 'completed')
        send('/goal clear'); checks.append('goal_blocked_user_input_and_evidence_completion')
        assert not status()['trials'], 'project Goals must not implicitly create Trials'
        finish('first-session'); ui = launch(); client = connect_latest(); second_id = status()['application_session_id']
        assert second_id != first_id; send('SECOND_SESSION_MARKER')
        count = len(Provider.requests); ui.send('/resume advanced first')
        original = clients[0]
        ui.until(lambda: original.call('status')['lease'] is not None and client.call('status')['lease'] is None)
        client = original; assert len(Provider.requests) == count
        # Lease acquisition precedes native terminal initialization. Wait for
        # the restored conversation, otherwise Enter can arrive during startup.
        ui.until(lambda: 'UNBLOCK_COMPLETE' in ui.text() and '⏺ update_goal(complete' in ui.text())
        send('AFTER_CROSS_HOST_RESUME')
        raw = json.dumps(Provider.requests[-1]); assert 'FORK_BRANCH_MARKER' in raw and 'SECOND_SESSION_MARKER' not in raw
        assert status()['application_session_id'] == first_id
        checks.append('resume_original_host_no_replay_or_history_mix')
        finish('resumed-session')
        ui = Terminal([str(args.launcher.resolve()), 'resume'], project, env)
        ui.until(lambda: '搜索' in ui.text()); os.write(ui.master, b'\x1b'); ui.until(lambda: ui.child.poll() is not None)
        assert ui.child.returncode == 0; checks.append('cli_resume_picker_cancel_no_execution')
        ui.close(output / 'resume-picker.txt'); ui = None
        (output / 'scenario.json').write_text(json.dumps({'status': 'passed', 'checks': checks, 'requests': len(Provider.requests)}, indent=2) + '\n')
        print(json.dumps({'status': 'passed', 'checks': checks}))
    finally:
        if ui: (output / 'last-transcript.txt').write_bytes(ui.transcript)
        (output / 'requests.json').write_text(json.dumps(Provider.requests, ensure_ascii=False))
        for run in home.glob('runs/*'): cleanup_fixture_hosts(run)
        if ui:
            try: ui.close(output / 'last-transcript.txt')
            except Exception: pass
        for client_ in clients: client_.close()
        server.shutdown()


if __name__ == '__main__': main()
