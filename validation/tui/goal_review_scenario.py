"""Native Pi Goal: fresh reviewer continues, completes, and honors a user pause."""
import argparse
import io
import json
import os
from pathlib import Path
from http.server import ThreadingHTTPServer
import threading
import time
import uuid

from advanced_scenario import Provider
from codegen_edges import Terminal
from product_scenario import ROOT, cleanup_fixture_hosts
from pty_scenario import Client


class ReviewProvider(Provider):
    reviews = []
    reviewing_pause = threading.Event()
    def do_POST(self):
        data = self.rfile.read(int(self.headers['Content-Length']))
        body = json.loads(data)
        if body['messages'][0]['content'].startswith('You independently assess a coding Goal'):
            self.reviews.append(body)
            assert not body.get('tools') and len(body['messages']) == 2
            if 'REVIEW_PAUSE' in body['messages'][1]['content']:
                self.reviewing_pause.set(); time.sleep(1.5)
            status = 'continue' if len(self.reviews) == 1 else 'met'
            text = json.dumps({'status': status, 'summary': 'Need one additional retained read' if status == 'continue' else 'Original read evidence verified'})
            if not body.get('stream'):
                self.send_response(200); self.send_header('Content-Type', 'application/json'); self.end_headers()
                self.wfile.write(json.dumps({'choices': [{'message': {'role': 'assistant', 'content': text}, 'finish_reason': 'stop'}],
                    'usage': {'prompt_tokens': 50, 'completion_tokens': 10}}).encode())
                return
            self.send_response(200); self.send_header('Content-Type', 'text/event-stream'); self.end_headers()
            for chunk in [{'choices': [{'index': 0, 'delta': {'role': 'assistant', 'content': text}}]},
                          {'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}], 'usage': {'prompt_tokens': 50, 'completion_tokens': 10}}]:
                self.wfile.write(('data: ' + json.dumps(chunk) + '\n\n').encode()); self.wfile.flush()
            self.wfile.write(b'data: [DONE]\n\n')
        else:
            self.rfile = io.BytesIO(data)
            super().do_POST()


def main():
    parser = argparse.ArgumentParser(); parser.add_argument('--output', type=Path, required=True)
    output = parser.parse_args().output.resolve(); output.mkdir(exist_ok=False)
    project = output / 'project'; project.mkdir(); (project / 'solution.py').write_text('x = 1\n')
    server = ThreadingHTTPServer(('127.0.0.1', 0), ReviewProvider)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    config = output / 'config.json'; config.write_text(json.dumps({'providers': {'primary': {
        'model': 'review-fixture', 'base_url': f'http://127.0.0.1:{server.server_port}/v1', 'api_key_env': 'KERNELBRIDGE_TEST_KEY'}},
        'goal_review': {'enabled': True}}))
    env = {**os.environ, 'TERM': 'xterm-256color', 'KERNELBRIDGE_TEST_KEY': 'fixture-only',
        'KERNELBRIDGE_HOME': str(output / 'home'), 'KERNELBRIDGE_SESSIONS': str(Path('/tmp').resolve() / ('kb-review-' + uuid.uuid4().hex[:8]))}
    ui = Terminal([str(ROOT / 'kernelagent'), '--config', str(config)], project, env); client = None
    try:
        ui.until(lambda: '等待输入' in ui.text())
        run = next((output / 'home/runs').iterdir()); binding = json.loads((run / 'application.json').read_text())
        client = Client(Path(binding['host_directory']))
        assert not ReviewProvider.reviews
        ui.send('/goal READ_STEP and verify the retained read')
        ui.until(lambda: (client.call('status')['goal'] or {}).get('status') == 'completed')
        state = client.call('status')
        assert len(ReviewProvider.reviews) == 2
        assert state['goal']['completion_kind'] == 'review' and state['goal_review']['status'] == 'met'
        assert state['goal_usage']['model_turns'] == len(Provider.requests) + 2
        assert not state['pending_effect_ids'] and not state['trials']
        ui.send('/goal READ_STEP REVIEW_PAUSE')
        ui.until(ReviewProvider.reviewing_pause.is_set)
        client.call('control', {'input_id': str(uuid.uuid4()), 'operation': 'pause', 'channel': 'sdk'})
        ui.until(lambda: client.call('status')['goal']['status'] == 'paused' and not client.call('status')['pending_effect_ids'])
        ui.send('/quit'); ui.until(lambda: ui.child.poll() is not None); assert ui.child.returncode == 0
        result = {'status': 'passed', 'checks': ['ordinary_idle_no_review', 'fresh_toolfree_verdict', 'continue_then_complete',
            'review_usage_shared', 'pause_wins_over_late_verdict'], 'reviews': len(ReviewProvider.reviews)}
        (output / 'scenario.json').write_text(json.dumps(result, indent=2) + '\n'); print(json.dumps(result))
    finally:
        (output / 'review-requests.json').write_text(json.dumps(ReviewProvider.reviews))
        for run in (output / 'home/runs').glob('*'): cleanup_fixture_hosts(run)
        ui.close(output / 'transcript.txt')
        if client: client.close()
        server.shutdown()


if __name__ == '__main__': main()
