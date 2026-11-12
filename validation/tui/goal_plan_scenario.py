"""Native Pi Goal plan: update_plan exists only under a Goal, renders as a checklist,
refusals reach the model, the footer shows progress, status returns the latest plan,
and the fresh completion reviewer receives the final plan. Scripted local provider only."""
import argparse
import json
import os
import re
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import uuid

from codegen_edges import Terminal
from product_scenario import ROOT, cleanup_fixture_hosts
from pty_scenario import Client

FIRST = [{'step': '读取 solution.py', 'status': 'in_progress'}, {'step': '核对结果', 'status': 'pending'}]
INVALID = [{'step': '读取 solution.py', 'status': 'in_progress'}, {'step': '核对结果', 'status': 'in_progress'}]
SECOND = [{'step': '读取 solution.py', 'status': 'completed'}, {'step': '核对结果', 'status': 'in_progress'}]


class PlanProvider(BaseHTTPRequestHandler):
    requests, reviews = [], []
    release_review = threading.Event()

    def log_message(self, *args): pass

    def reply(self, body, text='', calls=()):
        tool_calls = [{'index': i, 'id': f'call_{len(self.requests)}_{i}', 'type': 'function',
                       'function': {'name': name, 'arguments': json.dumps(args, ensure_ascii=False)}} for i, (name, args) in enumerate(calls)]
        usage = {'prompt_tokens': 200, 'completion_tokens': 20}
        if not body.get('stream'):
            message = {'role': 'assistant', 'content': text, **({'tool_calls': [{k: v for k, v in c.items() if k != 'index'} for c in tool_calls]} if tool_calls else {})}
            self.send_response(200); self.send_header('Content-Type', 'application/json'); self.end_headers()
            self.wfile.write(json.dumps({'choices': [{'message': message, 'finish_reason': 'tool_calls' if calls else 'stop'}], 'usage': usage}).encode())
            return
        self.send_response(200); self.send_header('Content-Type', 'text/event-stream'); self.end_headers()
        delta = {'role': 'assistant', 'content': text, **({'tool_calls': tool_calls} if tool_calls else {})}
        for chunk in [{'choices': [{'index': 0, 'delta': delta}]},
                      {'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'tool_calls' if calls else 'stop'}], 'usage': usage}]:
            self.wfile.write(('data: ' + json.dumps(chunk, ensure_ascii=False) + '\n\n').encode()); self.wfile.flush()
        self.wfile.write(b'data: [DONE]\n\n')

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        messages = body['messages']
        if str(messages[0].get('content', '')).startswith('You independently assess a coding Goal'):
            self.reviews.append(body)
            self.release_review.wait(60)
            return self.reply(body, json.dumps({'status': 'met', 'summary': 'Read verified; final plan reviewed.'}))
        tools = sorted(tool['function']['name'] for tool in body.get('tools', []))
        self.requests.append({'tools': tools, 'messages': len(messages)})
        if not any('PLAN_GOAL' in json.dumps(m.get('content', ''), ensure_ascii=False) for m in messages if m.get('role') == 'user'):
            return self.reply(body, 'PLAIN_DONE')
        results = sum(1 for m in messages if m.get('role') == 'tool')
        if results == 0:
            return self.reply(body, '先列计划。', [('update_plan', {'explanation': '两步完成', 'plan': FIRST}), ('read', {'path': 'solution.py'})])
        if results == 2:
            return self.reply(body, '', [('update_plan', {'plan': INVALID})])
        if results == 3:
            return self.reply(body, '', [('update_plan', {'plan': SECOND})])
        return self.reply(body, 'PLAN_DONE 已读取并核对。')


def main():
    parser = argparse.ArgumentParser(); parser.add_argument('--output', type=Path, required=True)
    output = parser.parse_args().output.resolve(); output.mkdir(exist_ok=False)
    project = output / 'project'; project.mkdir(); (project / 'solution.py').write_text('x = 1\n')
    server = ThreadingHTTPServer(('127.0.0.1', 0), PlanProvider)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    config = output / 'config.json'; config.write_text(json.dumps({'providers': {'primary': {
        'model': 'plan-fixture', 'base_url': f'http://127.0.0.1:{server.server_port}/v1', 'api_key_env': 'KERNELBRIDGE_TEST_KEY'}},
        'goal_review': {'enabled': True}}))
    env = {**os.environ, 'TERM': 'xterm-256color', 'KERNELBRIDGE_TEST_KEY': 'fixture-only',
        'KERNELBRIDGE_HOME': str(output / 'home'), 'KERNELBRIDGE_SESSIONS': str(Path('/tmp').resolve() / ('kb-plan-' + uuid.uuid4().hex[:8]))}
    ui = Terminal([str(ROOT / 'kernelagent'), '--config', str(config)], project, env); client = None
    checks = []
    try:
        ui.until(lambda: '等待输入' in ui.text())
        client = Client(Path(json.loads((next((output / 'home/runs').iterdir()) / 'application.json').read_text())['host_directory']))
        ui.send('ordinary question'); ui.until(lambda: 'PLAIN_DONE' in ui.text() and '等待输入' in ui.text())
        assert 'update_plan' not in PlanProvider.requests[0]['tools'], PlanProvider.requests[0]['tools']
        checks.append('no_plan_tool_without_a_goal')
        ui.send('/goal PLAN_GOAL read and check solution.py')
        ui.until(lambda: PlanProvider.reviews, timeout=120)
        goal_requests = [r for r in PlanProvider.requests[1:]]
        assert goal_requests and all('update_plan' in r['tools'] for r in goal_requests), goal_requests
        checks.append('plan_tool_offered_under_a_goal')
        screen = ui.text()
        # Styled cells interleave escape codes with text; compare the visible characters.
        transcript = re.sub(r'\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07', '', ui.transcript.decode(errors='replace'))
        (output / 'plan-screen.txt').write_text(screen)
        assert '更新计划' in screen and '■ 读取 solution.py' in transcript and '□ 核对结果' in transcript, screen
        assert '失败：at most one plan step may be in_progress' in transcript, screen
        assert '✔ 读取 solution.py' in transcript and '■ 核对结果' in transcript
        checks.append('plan_cells_render_and_refusal_reaches_the_screen')
        assert '计划 1/2 · 正在：核对结果' in screen, screen
        state = client.call('status')
        assert state['plan']['plan'] == SECOND and state['plan']['completed'] == 1 and state['plan']['total'] == 2, state['plan']
        checks.append('footer_and_status_show_the_latest_plan')
        review = PlanProvider.reviews[0]['messages'][1]['content']
        assert '"plan":{"explanation":null,"steps":[' in review and '"status":"in_progress","step":"核对结果"' in review, review[:400]
        assert 'is not evidence' in PlanProvider.reviews[0]['messages'][0]['content']
        checks.append('reviewer_receives_the_final_plan')
        PlanProvider.release_review.set()
        ui.until(lambda: (client.call('status')['goal'] or {}).get('status') == 'completed', timeout=60)
        ui.send('/quit'); ui.until(lambda: ui.child.poll() is not None); assert ui.child.returncode == 0
        result = {'status': 'passed', 'checks': checks, 'model_requests': len(PlanProvider.requests), 'reviews': len(PlanProvider.reviews)}
        (output / 'scenario.json').write_text(json.dumps(result, indent=2) + '\n'); print(json.dumps(result))
    finally:
        PlanProvider.release_review.set()
        (output / 'requests.json').write_text(json.dumps({'requests': PlanProvider.requests, 'reviews': PlanProvider.reviews}, ensure_ascii=False))
        for run in (output / 'home/runs').glob('*'): cleanup_fixture_hosts(run)
        ui.close(output / 'transcript.txt')
        if client: client.close()
        server.shutdown()


if __name__ == '__main__': main()
