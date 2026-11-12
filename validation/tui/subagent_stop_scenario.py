"""Real Pi: stop one background child from /agents, then continue the parent."""
import argparse
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import threading
import uuid

from product_scenario import ROOT, cleanup_fixture_hosts
from pty_scenario import Client
import subagent_parallel


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--launcher', type=Path, default=ROOT / 'kernelagent')
    args = parser.parse_args(); output = args.output.resolve(); output.mkdir(parents=True, exist_ok=False)
    project = output / 'project'; project.mkdir(); (project / 'solution.py').write_text('def add(a, b):\n    return a + b\n')
    subagent_parallel.DELAY = 30
    server = ThreadingHTTPServer(('127.0.0.1', 0), subagent_parallel.Provider)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    config = output / 'config.json'; config.write_text(json.dumps({'providers': {'primary': {
        'model': 'stop-fixture', 'base_url': f'http://127.0.0.1:{server.server_port}/v1', 'api_key_env': 'KERNELBRIDGE_TEST_KEY'}}}))
    home = output / 'home'
    env = {**os.environ, 'TERM': 'xterm-256color', 'PYTHONDONTWRITEBYTECODE': '1',
           'KERNELBRIDGE_HOME': str(home), 'KERNELBRIDGE_CONFIG': str(config), 'KERNELBRIDGE_TEST_KEY': 'fixture',
           'KERNELBRIDGE_SESSIONS': str(Path('/tmp').resolve() / ('kb-stop-' + uuid.uuid4().hex[:8]))}
    ui = subagent_parallel.Terminal([str(args.launcher.resolve())], project, env, rows=40, columns=48)
    client = None
    try:
        ui.until(lambda: '/help' in ui.text() and '等待输入' in ui.text())
        binding = json.loads(next(home.glob('runs/*/application.json')).read_text()); client = Client(Path(binding['host_directory']))
        ui.send('BACKGROUND_START')
        ui.until(lambda: '后台任务运行中' in ui.text() and '等待 1 个子代理完成' in ui.text(), timeout=60)
        child_id = client.call('status')['subagents'][0]['id']
        ui.send('/agents'); ui.until(lambda: '本会话（1）' in ui.text())
        for _ in client.call('status')['subagent_types']:
            os.write(ui.master, b'\x1b[B'); ui.pump()
        ui.until(lambda: 's 停止' in ui.text())
        (output / 'selected-screen.txt').write_text(ui.text())
        os.write(ui.master, b's')
        ui.until(lambda: '已请求停止' in ui.text(), timeout=30)

        def stopped():
            state = client.call('status')
            child = state['subagents'][0]
            notifications = [row for row in state['inputs'].values() if row['initiator'] == 'agent']
            return (child['id'] == child_id and child['status'] == 'failed' and child['reason'] == 'stopped'
                    and not state['pending_effect_ids'] and not state['calls']
                    and len(notifications) == 1 and notifications[0]['status'] == 'completed')

        ui.until(stopped, timeout=90); ui.until(lambda: '等待输入' in ui.text())
        (output / 'stopped-screen.txt').write_text(ui.text())
        ui.send('AFTER_CANCEL')
        ui.until(lambda: 'AFTER_CANCEL_REPLY' in ui.text() and '等待输入' in ui.text(), timeout=60)
        state = client.call('status')
        assert len(state['subagents']) == 1 and state['subagents'][0]['id'] == child_id
        assert state['budget']['reserved'] == 0 and not state['pending_effect_ids']
        assert len([row for row in state['inputs'].values() if row['text'] == 'AFTER_CANCEL' and row['status'] == 'completed']) == 1
        assert (project / 'solution.py').read_text() == 'def add(a, b):\n    return a + b\n'
        (output / 'final-state.json').write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\n')
        ui.send('/quit'); ui.until(lambda: ui.child.poll() is not None); assert ui.child.returncode == 0
        result = {'status': 'passed', 'checks': ['agents_s_stops_selected_background_child',
                  'stopped_child_notifies_parent_once', 'parent_accepts_next_prompt_after_child_stop'],
                  'scope': 'real Pi at 48 columns, scripted local provider, no device'}
        (output / 'scenario.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
        print(json.dumps(result), flush=True)
    finally:
        (output / 'last-screen.txt').write_text(ui.text())
        ui.close(output / 'transcript.txt')
        if client: client.close()
        for run in home.glob('runs/*'): cleanup_fixture_hosts(run)
        server.shutdown(); server.server_close()


if __name__ == '__main__': main()
