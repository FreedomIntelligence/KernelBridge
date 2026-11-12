"""Branch views in the live Lattice viewer, rendered in headless Chrome.

A Pi session file whose tree has an abandoned branch (opened with lattice-trace)
and a KernelBridge Journal with a /fork from a historical entry (served by the
/trace server) must both render every tab, in both branch scopes, with a step
selected on branch B, without page errors. No model, provider or device is used.
"""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
TABS = ['overview', 'timeline', 'history', 'workspace', 'decisions', 'context', 'files', 'compare', 'agent', 'live-progress']
T0 = 1_794_000_000_000


def iso(ms):
    return time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime((T0 + ms) / 1000)) + '.%03dZ' % ((T0 + ms) % 1000)


def pi_session(path):
    usage = lambda i, o, c: {'input': i, 'output': o, 'cacheRead': c, 'cacheWrite': 0, 'totalTokens': i + o + c, 'cost': {'total': 0}}
    rows, parent = [{'type': 'session', 'version': 3, 'id': 'sess-branches', 'timestamp': iso(0), 'cwd': '/work'}], None
    def add(id, ms, message, parent_id=None, end=None):
        nonlocal parent
        rows.append({'type': 'message', 'id': id, 'parentId': parent if parent_id is None else parent_id, 'timestamp': iso(end or ms),
                     'message': {'timestamp': T0 + ms, **message}})
        parent = id
    add('u1', 100, {'role': 'user', 'content': [{'type': 'text', 'text': 'Fix the parser'}]})
    add('a1', 200, {'role': 'assistant', 'provider': 'f', 'model': 'fixture', 'stopReason': 'toolUse', 'usage': usage(400, 30, 800),
        'content': [{'type': 'toolCall', 'id': 'c1', 'name': 'read', 'arguments': {'path': 'parser.py'}}]}, end=600)
    add('r1', 700, {'role': 'toolResult', 'toolCallId': 'c1', 'toolName': 'read', 'isError': False, 'content': [{'type': 'text', 'text': 'def parse(): ...'}]})
    add('as', 710, {'role': 'assistant', 'provider': 'f', 'model': 'fixture', 'stopReason': 'toolUse', 'usage': usage(100, 10, 1100),
        'content': [{'type': 'toolCall', 'id': 'sub', 'name': 'subagent', 'arguments': {'agent': 'scout', 'task': 'Find callers'}}]}, end=720)
    child = [{'role': 'user', 'content': [{'type': 'text', 'text': 'Find callers'}], 'timestamp': T0 + 730},
             {'role': 'assistant', 'provider': 'f', 'model': 'fixture', 'stopReason': 'stop', 'usage': usage(80, 8, 0),
              'content': [{'type': 'text', 'text': 'Only main.py.'}], 'timestamp': T0 + 740}]
    add('rs', 780, {'role': 'toolResult', 'toolCallId': 'sub', 'toolName': 'subagent', 'isError': False, 'content': [{'type': 'text', 'text': 'done'}],
        'details': {'mode': 'single', 'results': [{'index': 0, 'agent': 'scout', 'task': 'Find callers', 'exitCode': 0, 'usage': {}, 'finalOutput': 'Only main.py.', 'messages': child}]}})
    add('ub', 800, {'role': 'user', 'content': [{'type': 'text', 'text': 'Rewrite it from scratch'}]})
    add('ab', 900, {'role': 'assistant', 'provider': 'f', 'model': 'fixture', 'stopReason': 'stop', 'usage': usage(200, 40, 1200),
        'content': [{'type': 'text', 'text': 'Rewritten.'}]}, end=1500)
    add('u2', 1600, {'role': 'user', 'content': [{'type': 'text', 'text': 'Patch the edge case instead'}]}, parent_id='rs')
    add('a2', 1700, {'role': 'assistant', 'provider': 'f', 'model': 'fixture', 'stopReason': 'toolUse', 'usage': usage(150, 20, 1250),
        'content': [{'type': 'toolCall', 'id': 'c2', 'name': 'edit', 'arguments': {'path': 'parser.py', 'edits': [{'oldText': '...', 'newText': 'return []'}]}}]}, end=2000)
    add('r2', 2100, {'role': 'toolResult', 'toolCallId': 'c2', 'toolName': 'edit', 'isError': False, 'content': [{'type': 'text', 'text': 'Edited parser.py'}]})
    add('a3', 2200, {'role': 'assistant', 'provider': 'f', 'model': 'fixture', 'stopReason': 'stop', 'usage': usage(100, 15, 1400),
        'content': [{'type': 'text', 'text': 'Edge case fixed.'}]}, end=2500)
    path.write_text(''.join(json.dumps(row) + '\n' for row in rows))


def kb_journal(path):
    rows, clock = [], 1_800_000_000_000_000_000
    def add(row): rows.append({'seq': len(rows), **row})
    def app(kind, data):
        nonlocal clock; clock += 100_000_000
        add({'kind': kind, 'application_session_id': 'application-branches', 'data': data, 'time_ns': clock})
    def turn(id, text, prompt):
        app('application/call_intent', {'call_id': id, 'method': 'model'})
        add({'kind': 'admitted', 'action': {'id': id, 'tool': '__propose__', 'task': 't', 'arguments': {}, 'parents': []}})
        add({'kind': 'started', 'action_id': id})
        add({'kind': 'settled', 'action_id': id, 'elapsed_ms': 50, 'outcome': {'status': 'completed',
             'data': {'content': [{'type': 'text', 'text': text}], 'usage': {'prompt_tokens': prompt, 'completion_tokens': 20}}}})
        app('application/call_result', {'call_id': id, 'result': {}})
    add({'kind': 'task', 'task': {'id': 't', 'goal': 'g'}})
    clock += 100_000_000
    add({'kind': 'atomic', 'time_ns': clock, 'events': [
        {'kind': 'application/input_accepted', 'application_session_id': 'application-branches', 'data': {'kind': 'prompt', 'initiator': 'user', 'text': 'first'}},
        {'kind': 'application/goal', 'application_session_id': 'application-branches', 'data': {'goal_id': 'g1', 'objective': 'Fix the parser', 'status': 'active', 'started_at_ns': clock}}]})
    turn('t/step/0/model/primary', 'One.', 1000)
    plan = 't/step/0/model/primary/call/0'
    add({'kind': 'admitted', 'action': {'id': plan, 'tool': 'update_plan', 'task': 't', 'parents': ['t/step/0/model/primary'],
         'arguments': {'explanation': 'Two steps', 'plan': [{'step': 'Read parser.py', 'status': 'completed'}, {'step': 'Fix the edge case', 'status': 'in_progress'}]}}})
    add({'kind': 'started', 'action_id': plan})
    add({'kind': 'settled', 'action_id': plan, 'elapsed_ms': 5, 'outcome': {'status': 'completed', 'data': {'result': {'isError': False}}}})
    app('application/checkpoint', {'pi_session_id': 'p1', 'entry_id': 'e1', 'operation': 'checkpoint'})
    app('application/input_accepted', {'kind': 'prompt', 'initiator': 'user', 'text': 'abandoned idea'})
    turn('t/step/1/model/primary', 'Abandoned.', 1200)
    app('application/configuration', {'operation': 'transition_prepared', 'parent': {'operation': 'fork', 'pi_session_id': 'p1', 'entry_id': 'e1'}})
    app('application/input_accepted', {'kind': 'prompt', 'initiator': 'user', 'text': 'better idea'})
    turn('t/step/2/model/primary', 'Better.', 1100)
    # A subagent still running: spawn and child steps exist, the return does not yet.
    delegate = 't/step/2/model/primary/call/0'
    add({'kind': 'admitted', 'action': {'id': delegate, 'tool': 'delegate', 'task': 't', 'arguments': {'agent': 'kernel-scout', 'task': 'Find callers'}, 'parents': ['t/step/2/model/primary']}})
    add({'kind': 'started', 'action_id': delegate})
    add({'kind': 'task', 'task': {'id': delegate + '/leaf', 'goal': 'Find callers'}})
    child = delegate + '/leaf/step/0/model/primary'
    add({'kind': 'admitted', 'action': {'id': child, 'tool': '__propose__', 'task': delegate + '/leaf', 'arguments': {}, 'parents': []}})
    add({'kind': 'started', 'action_id': child})
    add({'kind': 'settled', 'action_id': child, 'elapsed_ms': 40, 'outcome': {'status': 'completed', 'data': {'content': [{'type': 'text', 'text': 'Searching.'}],
         'usage': {'prompt_tokens': 500, 'completion_tokens': 10}}}})
    app('application/call_result', {'call_id': 'child', 'result': {}})
    path.write_text(''.join(json.dumps(row) + '\n' for row in rows))


def serve(command):
    process = subprocess.Popen(command, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    deadline = time.time() + 30
    while time.time() < deadline:
        line = process.stdout.readline()
        found = re.search(r'http://127\.0\.0\.1:\d+/[0-9a-f]{32}/\S*', line or '')
        if found: return process, found.group(0)
        if process.poll() is not None: break
    process.kill(); raise AssertionError('viewer did not start: ' + ' '.join(command))


def render(url, label, output):
    from playwright.sync_api import sync_playwright
    errors = []
    with sync_playwright() as playwright:
        try: browser = playwright.chromium.launch(channel='chrome', headless=True)
        except Exception: browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width': 1440, 'height': 900})
        page.on('pageerror', lambda error: errors.append(str(error)))
        # The viewer catches render errors itself and logs them; they count as failures too.
        page.on('console', lambda message: errors.append(message.text) if message.type == 'error' and 'Failed to load resource' not in message.text else None)
        page.goto(url); page.wait_for_function('typeof STEPS !== "undefined" && STEPS.length > 0', timeout=15000)
        state = page.evaluate('({live: !!DS.live, fork: FORK && FORK.step, forkb: FORKB.slice(), rows: ROWS.slice(), subs: SUBS.slice()})')
        assert state['live'] and state['fork'] and state['forkb'] == ['B'] and 'B/main' in state['rows'], state
        assert state['subs'] == ['M/c1'], state  # one subagent lane on the current branch in both sources
        for scope in ['M', 'B']:
            page.evaluate(f'state.scope = {json.dumps(scope)}; renderAll()')
            for tab in TABS:
                page.evaluate(f'setTab({json.dumps(tab)})'); page.wait_for_timeout(150)
        page.evaluate('state.sel = STEPS.filter(function(s){ return s.branch === "B"; }).slice(-1)[0].id; state.scope = "B"; setTab("timeline")')
        page.wait_for_timeout(200)
        page.evaluate('setTab("compare")'); page.wait_for_timeout(200)
        compare = page.evaluate('document.getElementById("p-compare").innerText')
        # 进展: the module is enabled for the live session and shows its Goal and plan (or says there is none).
        page.evaluate('setTab("live-progress")'); page.wait_for_timeout(200)
        progress = page.evaluate('({shown: !document.getElementById("p-kbprog").hidden, text: document.getElementById("p-kbprog").innerText,'
                                 ' tab: state.tab, module: state.module})')
        page.screenshot(path=str(output / f'{label}-progress.png'))
        if label == 'kernelbridge':
            page.evaluate('document.querySelector("#p-kbprog [data-goto]").click()'); page.wait_for_timeout(200)
            selected = page.evaluate('state.sel')
        page.screenshot(path=str(output / f'{label}-compare.png'))
        browser.close()
    assert not errors, (label, errors)
    assert 'B' in compare and '没有 fork 分支' not in compare, (label, compare[:300])
    assert progress['shown'] and progress['tab'] == 'live-progress' and progress['module'] == 'm-prog', progress
    assert '进度曲线' in progress['text'], progress['text'][:300]
    if label == 'kernelbridge':
        assert 'Fix the parser' in progress['text'] and '当前计划 1/2' in progress['text'] and 'Read parser.py' in progress['text'], progress['text'][:500]
        assert selected and selected.startswith('T'), selected  # 跳到步骤 selects the update_plan step
    else:
        assert '这个会话没有设置 Goal' in progress['text'], progress['text'][:300]
    return f'{label}_branches_render_without_errors'


def main():
    parser = argparse.ArgumentParser(); parser.add_argument('--output', type=Path, required=True)
    output = parser.parse_args().output.resolve(); output.mkdir(parents=True, exist_ok=False)
    try:
        import playwright  # noqa: F401
    except ImportError:
        print(json.dumps({'status': 'skipped', 'reason': 'playwright not installed'})); return 0
    checks = []
    session = output / 'branches.jsonl'; pi_session(session)
    process, url = serve(['node', 'source/packages/pi-lattice-trace/bin/lattice-trace.ts', str(session), '--no-open'])
    try: checks.append(render(url, 'pi', output))
    finally: process.kill()
    journal = output / 'events.jsonl'; kb_journal(journal)
    script = ("import { startTraceServer } from './source/apps/agent/src/trace/server.ts';"
              f"const server = await startTraceServer({{ journal: {json.dumps(str(journal))} }}); console.log(server.url); setInterval(() => {{}}, 1000);")
    process, url = serve(['node', '--input-type=module', '-e', script])
    try: checks.append(render(url, 'kernelbridge', output))
    finally: process.kill()
    result = {'status': 'passed', 'checks': checks}
    (output / 'scenario.json').write_text(json.dumps(result, indent=2) + '\n'); print(json.dumps(result))
    return 0


if __name__ == '__main__': sys.exit(main())
