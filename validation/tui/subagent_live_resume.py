"""Finish a retained live background phase without re-dispatching its original input."""
import argparse
import json
import os
from pathlib import Path
import time

from codegen_edges import Terminal
from product_scenario import ROOT, cleanup_fixture_hosts
from pty_scenario import Client


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--recover-from', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args(); original = args.recover_from.resolve(); output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    run = original / 'run'; project = original / 'project'
    binding = json.loads((run / 'application.json').read_text()); host = Path(binding['host_directory'])
    markers = [(project / directory / 'verification.txt').read_text().strip() for directory in ('contract', 'recovery')]
    contents = {path.relative_to(project): path.read_bytes() for path in project.rglob('*') if path.is_file()}
    env = {**os.environ, 'TERM': 'xterm-256color', 'PYTHONDONTWRITEBYTECODE': '1', 'KERNELBRIDGE_SESSIONS': str(host.parent)}
    for name in ('DEEPSEEK_API_KEY', 'KERNELBRIDGE_HOME', 'KERNELBRIDGE_CONFIG'): env.pop(name, None)
    ui = Terminal([str(ROOT / 'kernelagent'), 'resume', binding['application_session_id']], project, env)
    observations = []; running_seen = False; foreground_returned = False; client = None
    continuation = '继续上一条尚未启动的后台请求，按原要求启动两个只读后台子代理读取新 verification.txt，等两份结果后汇总并输出原要求的完成标识。之前四个已完成的子任务不要重跑。'
    continuation_sent = False
    try:
        connect_deadline = time.monotonic() + 120
        while client is None and time.monotonic() < connect_deadline:
            ui.pump()
            if ui.child.poll() is not None: raise AssertionError(ui.text())
            try: client = Client(host)
            except (ConnectionRefusedError, FileNotFoundError): pass
        assert client is not None, 'resumed host did not open its socket'
        client.socket.settimeout(120)
        before = client.call('status'); retained = {row['id']: row for row in before['subagents']}
        original_input = next(row for row in before['inputs'].values() if row['initiator'] == 'user' and 'BACKGROUND_PAIR_COMPLETE' in row['text'])
        assert original_input['status'] in {'queued', 'applied', 'interrupted'}, original_input
        assert all(row['status'] == 'completed' for row in retained.values())
        deadline = time.monotonic() + 900
        while time.monotonic() < deadline:
            tick = time.monotonic()
            while time.monotonic() - tick < .5: ui.pump()
            state = client.call('status')
            if (not continuation_sent and state['inputs'][original_input['input_id']]['status'] == 'interrupted'
                    and '等待输入' in ui.text()):
                ui.send(continuation); continuation_sent = True
            active_input = next((row for row in state['inputs'].values() if row['text'] == continuation), None) if continuation_sent else state['inputs'][original_input['input_id']]
            children = [row for row in state['subagents'] if row['id'] not in retained]
            notifications = [row for key, row in state['inputs'].items() if key not in before['inputs'] and row['initiator'] == 'agent']
            facts = {'children': [{key: row.get(key) for key in ('id', 'status', 'background', 'tool_calls', 'steps')} for row in children],
                     'input_status': active_input['status'] if active_input else 'queued',
                     'notifications': [(row['input_id'], row['status']) for row in notifications]}
            if not observations or facts != observations[-1]:
                observations.append(facts); (output / 'progress.json').write_text(json.dumps(observations, ensure_ascii=False, indent=2) + '\n')
                (output / 'progress-screen.txt').write_text(ui.text())
            active = any(row['status'] == 'running' for row in children)
            running_seen |= active
            foreground_returned |= active and facts['input_status'] == 'completed'
            if (len(children) == 2 and all(row['status'] == 'completed' for row in children)
                    and len(notifications) == 2 and all(row['status'] == 'completed' for row in notifications)
                    and not state['pending_effect_ids'] and not state['calls']): break
            if ui.child.poll() is not None: raise AssertionError(ui.text())
        else: raise AssertionError('Retained background phase did not settle; original receipts retained')
        ui.until(lambda: '等待输入' in ui.text(), timeout=180)
        assert running_seen and foreground_returned
        assert all(row['background'] and row['tool_calls'] > 0 for row in children)
        assert len({row['input_id'] for row in notifications}) == 2
        assert len([row for row in state['inputs'].values() if row['text'] == original_input['text']]) == 1
        assert not continuation_sent or len([row for row in state['inputs'].values() if row['text'] == continuation]) == 1
        assert all(next(row for row in state['subagents'] if row['id'] == key)['summary'] == value['summary'] for key, value in retained.items())
        assert all(marker in '\n'.join(row['summary'] for row in children) for marker in markers)
        session_file = max((run / 'state/pi-sessions').glob('*/sessions/*.jsonl'), key=lambda path: path.stat().st_mtime_ns)
        entries = [json.loads(line) for line in session_file.read_text().splitlines()]
        answer = next(row['message'] for row in reversed(entries) if row.get('type') == 'message' and row['message'].get('role') == 'assistant')
        summary = '\n'.join(block['text'] for block in answer['content'] if block['type'] == 'text')
        assert 'BACKGROUND_PAIR_COMPLETE' in summary and all(marker in summary for marker in markers), summary
        assert state['budget']['limit'] is None and state['budget']['reserved'] == 0 and not state['goal'] and not state['trials']
        assert all((project / path).read_bytes() == data for path, data in contents.items())
        (output / 'parent-summary.txt').write_text(summary)
        (output / 'final-state.json').write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\n')
        panel_title = f'本会话（{len(retained) + 2}）'
        ui.send('/agents'); ui.until(lambda: panel_title in ui.text(), timeout=180)
        (output / 'agents-screen.txt').write_text(ui.text()); os.write(ui.master, b'\x1b')
        ui.until(lambda: panel_title not in ui.text(), timeout=180)
        ui.send('/quit'); ui.until(lambda: ui.child.poll() is not None, timeout=180)
        assert ui.child.returncode == 0
        result = {'status': 'passed', 'checks': ['interrupted_background_request_continued_without_resubmitting_original_prompt',
                  'completed_foreground_children_retained', 'two_live_background_children_notify_once_then_parent_synthesizes_new_evidence',
                  'parent_returns_while_background_children_run', 'native_agents_panel_and_no_file_mutation'],
                  'prior_phase_evidence': str(original), 'scope': 'real model + local source, no device evaluation'}
        (output / 'scenario.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n'); print(json.dumps(result), flush=True)
    finally:
        (output / 'last-screen.txt').write_text(ui.text()); (output / 'transcript.txt').write_bytes(ui.transcript)
        try: ui.close(output / 'transcript.txt')
        finally:
            if client: client.close()
            cleanup_fixture_hosts(run)


if __name__ == '__main__': main()
