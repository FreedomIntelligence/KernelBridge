"""Real Pi resume with default discovery: no KERNELBRIDGE_HOME/SESSIONS override."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import threading
import time
import uuid

from codegen_edges import Terminal
from product_scenario import ROOT, cleanup_fixture_hosts
from pty_scenario import Client


class Provider(BaseHTTPRequestHandler):
    requests = []

    def log_message(self, *_): pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.requests.append(body)
        content = 'HISTORY_REPLY_' + str(len(self.requests))
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.end_headers()
        chunks = [
            {'choices': [{'index': 0, 'delta': {'role': 'assistant', 'content': content}}]},
            {'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}],
             'usage': {'prompt_tokens': 100, 'completion_tokens': 10}},
        ]
        for chunk in chunks:
            self.wfile.write(('data: ' + json.dumps(chunk) + '\n\n').encode())
        self.wfile.write(b'data: [DONE]\n\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--launcher', type=Path, default=ROOT / 'kernelagent')
    args = parser.parse_args()
    output = args.output.resolve(); output.mkdir(parents=True, exist_ok=False)
    project = output / 'project'; project.mkdir()
    subprocess.run(['git', 'init', '-q', '-b', 'main', str(project)], check=True)
    other = output / 'other-project'; other.mkdir()
    subprocess.run(['git', 'init', '-q', '-b', 'other-branch', str(other)], check=True)
    # A short test-owned HOME keeps Darwin's Unix socket below its path limit.
    while True:
        home = Path('/tmp').resolve() / uuid.uuid4().hex[:4]
        try:
            home.mkdir(mode=0o700)
            break
        except FileExistsError:
            continue
    server = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    config = output / 'model.json'
    config.write_text(json.dumps({'providers': {'primary': {'model': 'resume-fixture',
        'base_url': f'http://127.0.0.1:{server.server_port}/v1', 'api_key_env': 'KERNELBRIDGE_TEST_KEY'}}}))
    env = {**os.environ, 'HOME': str(home), 'TERM': 'xterm-256color',
           'KERNELBRIDGE_CONFIG': str(config), 'KERNELBRIDGE_TEST_KEY': 'local-fixture'}
    for key in ['KERNELBRIDGE_HOME', 'KERNELBRIDGE_SESSIONS']: env.pop(key, None)
    launcher = str(args.launcher.resolve()); ui = None; clients = []; checks = []; launches = 0

    def launch(cwd, arguments=()):
        nonlocal ui, launches
        launches += 1
        ui = Terminal([launcher, *arguments], cwd, env)
        ui.until(lambda: '等待输入' in ui.text())

    def connect(run):
        binding = json.loads((run / 'application.json').read_text())
        assert Path(binding['host_directory']).parent == home / '.kernelbridge/sessions', binding
        client = Client(Path(binding['host_directory'])); clients.append(client)
        return client

    def send(client, text):
        before = set(client.call('status')['inputs']); ui.send(text)
        ui.until(lambda: any(key not in before and row['text'] == text and row['status'] == 'completed'
                            for key, row in client.call('status')['inputs'].items()))
        deadline = time.monotonic() + .3
        while time.monotonic() < deadline: ui.pump()

    def finish(label):
        nonlocal ui
        ui.send('/quit'); ui.until(lambda: ui.child.poll() is not None)
        assert ui.child.returncode == 0
        ui.close(output / (label + '-transcript.txt')); ui = None

    try:
        first_run = output / 'run-first'
        launch(project, ['--output', str(first_run)]); first = connect(first_run)
        first_id = first.call('status')['application_session_id']
        send(first, 'FIRST_PROJECT_CONSTRAINT')
        send(first, '!printf retained > keep.txt')
        send(first, '/name 保存的算子会话')
        finish('first'); first.close(); cleanup_fixture_hosts(first_run)
        other_run = output / 'run-other'
        launch(other, ['--output', str(other_run)]); second = connect(other_run)
        send(second, 'OTHER_PROJECT_CONSTRAINT'); send(second, '/name OTHER_PROJECT_TITLE'); finish('other')
        cleanup_fixture_hosts(other_run)
        checks.append('named_histories_persist_after_hosts_exit')

        empty_run = output / 'run-empty'
        launch(project, ['--output', str(empty_run)]); empty = connect(empty_run)
        assert len(Provider.requests) == 2
        ui.send('/resume'); ui.until(lambda: '保存的算子会话' in ui.text() and '[当前]' in ui.text())
        assert 'OTHER_PROJECT_TITLE' not in ui.text() and '当前项目没有历史会话' not in ui.text()
        assert '日志' in ui.text() and 'main' in ui.text()
        (output / 'project-picker.txt').write_text(ui.text())
        os.write(ui.master, b'\x01'); ui.until(lambda: 'OTHER_PROJECT_TITLE' in ui.text())
        os.write(ui.master, b'\x02'); ui.until(lambda: 'OTHER_PROJECT_TITLE' not in ui.text())
        os.write(ui.master, b'\x02'); ui.until(lambda: 'OTHER_PROJECT_TITLE' in ui.text())
        os.write(ui.master, b'\x01'); ui.until(lambda: '全部项目' not in ui.text())
        count = len(Provider.requests); ui.send('算子')
        ui.until(lambda: 'FIRST_PROJECT_CONSTRAINT' in ui.text() and 'HISTORY_REPLY_1' in ui.text())
        first = connect(first_run)
        assert first.call('status')['application_session_id'] == first_id
        assert empty.call('status')['lease'] is None
        assert len(Provider.requests) == count
        assert (project / 'keep.txt').read_text() == 'retained'
        checks.append('default_home_picker_offline_resume_original_history_no_replay')
        checks.append('current_session_metadata_and_project_branch_keyboard_filters')
        ui.send('/resume --all'); ui.until(lambda: 'OTHER_PROJECT_TITLE' in ui.text())
        (output / 'all-projects-picker.txt').write_text(ui.text())
        os.write(ui.master, b'\x1b')
        ui.until(lambda: any(row['text'] == '/resume --all' and row['status'] == 'completed'
                            for row in first.call('status')['inputs'].values()))
        assert len(Provider.requests) == count
        checks.append('all_projects_picker_cancel_without_execution')
        send(first, 'AFTER_RESUME_CONSTRAINT')
        wire = json.dumps(Provider.requests[-1])
        assert 'FIRST_PROJECT_CONSTRAINT' in wire and 'OTHER_PROJECT_CONSTRAINT' not in wire
        finish('resumed'); cleanup_fixture_hosts(first_run)
        checks.append('resumed_model_receives_original_conversation_only')

        # An abandoned idle startup must not steal --continue from real work.
        launch(project, ['--output', str(output / 'run-latest-empty')]); finish('latest-empty')
        count = len(Provider.requests)
        launch(project, ['--continue']); ui.until(lambda: 'AFTER_RESUME_CONSTRAINT' in ui.text())
        first = connect(first_run); assert first.call('status')['lease'] is not None
        assert len(Provider.requests) == count; finish('continued')
        checks.append('continue_skips_empty_startups')

        ui = Terminal([launcher, 'resume'], project, env)
        ui.until(lambda: '搜索' in ui.text()); ui.send('保存的算子会话')
        ui.until(lambda: 'AFTER_RESUME_CONSTRAINT' in ui.text() and 'HISTORY_REPLY_3' in ui.text())
        assert len(Provider.requests) == count; finish('cli-picker')
        checks.append('cli_search_selects_same_history_without_replay')
        result = {'status': 'passed', 'checks': checks, 'provider_requests': len(Provider.requests),
                  'home': str(home), 'registry_overrides': False, 'original_session': first_id}
        (output / 'scenario.json').write_text(json.dumps(result, indent=2, ensure_ascii=False) + '\n')
        print(json.dumps(result, ensure_ascii=False), flush=True)
    finally:
        if ui: ui.close(output / 'last-transcript.txt')
        for client in clients: client.close()
        for run in output.glob('run-*'): cleanup_fixture_hosts(run)
        (output / 'requests.json').write_text(json.dumps(Provider.requests, ensure_ascii=False))
        server.shutdown(); server.server_close()


if __name__ == '__main__': main()
