"""Interrupt a real upstream child in the native TUI, then continue its parent."""
import argparse
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import threading
import uuid

from advanced_scenario import Provider
from codegen_edges import Terminal
from product_scenario import ROOT, cleanup_fixture_hosts
from pty_scenario import Client


def main():
    parser = argparse.ArgumentParser(); parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(); output = args.output.resolve(); output.mkdir(exist_ok=False)
    project = output / 'project'; project.mkdir(); (project / 'solution.py').write_text('x = 1\n')
    server = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    config = output / 'config.json'; config.write_text(json.dumps({'providers': {'primary': {
        'model': 'cancel-fixture', 'base_url': f'http://127.0.0.1:{server.server_port}/v1', 'api_key_env': 'KERNELBRIDGE_TEST_KEY'}}}))
    env = {**os.environ, 'TERM': 'xterm-256color', 'KERNELBRIDGE_TEST_KEY': 'fixture-only',
        'KERNELBRIDGE_HOME': str(output / 'home'), 'KERNELBRIDGE_SESSIONS': str(Path('/tmp').resolve() / ('kb-child-' + uuid.uuid4().hex[:8]))}
    ui = Terminal([str(ROOT / 'kernelagent'), '--config', str(config)], project, env); client = None
    try:
        ui.until(lambda: '等待输入' in ui.text())
        run = next((output / 'home/runs').iterdir()); binding = json.loads((run / 'application.json').read_text())
        client = Client(Path(binding['host_directory']))
        ui.send('Please use a subagent: DELEGATE_SLOW')
        ui.until(lambda: Provider.children['CHILD_SLOW'] > 0)
        os.write(ui.master, b'\x1b')
        ui.until(lambda: any(x['text'].endswith('DELEGATE_SLOW') and x['status'] == 'interrupted' for x in client.call('status')['inputs'].values()))
        state = client.call('status')
        assert not state['pending_effect_ids'] and not state['calls'], state
        ui.send('PARENT_AFTER_CANCEL')
        ui.until(lambda: any(x['text'] == 'PARENT_AFTER_CANCEL' and x['status'] == 'completed' for x in client.call('status')['inputs'].values()))
        state = client.call('status'); assert state['subagents'][0]['status'] == 'failed', state['subagents']
        events = [json.loads(line) for line in (run / 'events.jsonl').read_text().splitlines()]
        assert any(e.get('outcome', {}).get('data', {}).get('reason') == 'cancelled' for e in events)
        assert not any(e.get('kind') == 'admitted' and e.get('action', {}).get('tool') == 'read_file' for e in events)
        assert (project / 'solution.py').read_text() == 'x = 1\n'
        ui.send('/quit'); ui.until(lambda: ui.child.poll() is not None); assert ui.child.returncode == 0
        result = {'status': 'passed', 'checks': ['child_interrupted', 'known_receipts_settled', 'parent_continues', 'no_child_write']}
        (output / 'scenario.json').write_text(json.dumps(result, indent=2) + '\n'); print(json.dumps(result))
    finally:
        (output / 'requests.json').write_text(json.dumps(Provider.requests))
        for run in (output / 'home/runs').glob('*'): cleanup_fixture_hosts(run)
        ui.close(output / 'transcript.txt')
        if client: client.close()
        server.shutdown()


if __name__ == '__main__': main()
