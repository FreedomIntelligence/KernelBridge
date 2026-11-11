"""Opt-in real-model delegation routing, foreground/background results and native Pi status."""
import argparse
import json
import os
from pathlib import Path
import time
import uuid

from codegen_edges import Terminal
from product_scenario import ROOT, cleanup_fixture_hosts
from pty_scenario import Client


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--launcher', type=Path, default=ROOT / 'kernelagent')
    parser.add_argument('--start-at', choices=('simple', 'explicit', 'background'), default='simple',
                        help='Run only the remaining phases after an independently verified earlier phase.')
    args = parser.parse_args()
    output = args.output.resolve(); output.mkdir(parents=True, exist_ok=False)
    project = output / 'project'; project.mkdir()
    files = {
        'README.md': 'Two independent subsystems: contract/ validates feature scatter; recovery/ recovers original remote jobs. Audit implementation against each README.\n',
        'contract/README.md': 'CONTRACT_ORCHID: scatter max/min groups by index; missing groups produce zero. Ties retain the first source position. Inputs must be finite.\n',
        'contract/implementation.py': 'def empty_group():\n    return 0\n\ndef choose_tie(positions):\n    return min(positions)\n\ndef accepts(value):\n    return True  # bug: finite input validation omitted\n',
        'recovery/README.md': 'RECOVERY_CEDAR: after uncertain dispatch, recover the original handle. Never launch a duplicate job.\n',
        'recovery/implementation.py': 'def recover(handle, read_status, launch):\n    if handle is None:\n        return {"status": "unknown", "reason": "missing original handle"}\n    return read_status(handle)\n',
        'recovery/notes.md': 'Recovery only observes retained handles; a timeout does not prove a launch failed. No device execution is required for this review.\n',
    }
    for name, text in files.items():
        path = project / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_text(text)
    run = output / 'run'
    env = {**os.environ, 'TERM': 'xterm-256color',
           'KERNELBRIDGE_SESSIONS': str(Path('/tmp').resolve() / ('kb-salive-' + uuid.uuid4().hex[:8]))}
    for name in ('DEEPSEEK_API_KEY', 'KERNELBRIDGE_HOME', 'KERNELBRIDGE_CONFIG'):
        env.pop(name, None)
    ui = Terminal([str(args.launcher.resolve()), '--output', str(run)], project, env)
    client = None; checks = []; observations = []; phase_facts = {}

    def send(label, prompt, *, background=False):
        before = client.call('status'); ui.send(prompt)
        deadline = time.monotonic() + (600 if background else 360)
        running_seen = False; foreground_returned = False
        while time.monotonic() < deadline:
            tick = time.monotonic()
            while time.monotonic() - tick < .5: ui.pump()
            state = client.call('status')
            stable = lambda children: [{key: value for key, value in row.items() if key != 'elapsed_ms'} for row in children]
            changed = not observations or stable(state['subagents']) != stable(observations[-1]['subagents'])
            if changed:
                observations.append({'phase': label, 'subagents': state['subagents']})
            if any(row['status'] == 'running' for row in state['subagents']):
                running_seen = True
                if changed and '子代理' in ui.text(): (output / (label + '-running-screen.txt')).write_text(ui.text())
            receipt = next((v for k, v in state['inputs'].items() if k not in before['inputs'] and v['text'] == prompt), None)
            if receipt and receipt['status'] in {'completed', 'failed', 'interrupted'}:
                assert receipt['status'] == 'completed', receipt
                new_children = state['subagents'][len(before['subagents']):]
                if any(row['status'] == 'running' for row in new_children): foreground_returned = True
                notifications = [v for k, v in state['inputs'].items() if k not in before['inputs'] and v['initiator'] == 'agent']
                if not state['pending_effect_ids'] and not state['calls'] and all(row['status'] != 'running' for row in new_children):
                    if not background or (len(notifications) == 2 and all(v['status'] == 'completed' for v in notifications)):
                        break
        else: raise AssertionError('Live prompt did not settle; original receipts retained')
        ui.until(lambda: '等待输入' in ui.text())
        assert not state['pending_effect_ids'] and state['budget']['reserved'] == 0
        assert state['budget']['limit'] is None and not state['goal'] and not state['trials']
        phase_facts[label] = {'foreground_returned_while_children_running': foreground_returned,
                              'notification_ids': [v['input_id'] for k, v in state['inputs'].items()
                                                   if k not in before['inputs'] and v['initiator'] == 'agent']}
        (output / (label + '-state.json')).write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\n')
        (output / (label + '-screen.txt')).write_text(ui.text())
        return state['subagents'][len(before['subagents']):], running_seen

    try:
        ui.until(lambda: '等待输入' in ui.text() and '/help · /latest' in ui.text())
        binding = json.loads((run / 'application.json').read_text()); client = Client(Path(binding['host_directory']))
        client.socket.settimeout(120)
        assert client.call('status')['model_turns'] == 0
        if args.start_at == 'simple':
            children, _ = send('simple', '读取 README.md，只说项目包含哪两个子目录。')
            assert not children, children
            checks.append('simple_lookup_stays_in_parent')
            children, running = send('automatic', '请完整审查两个相互独立的子系统 contract/ 与 recovery/：分别核对文档契约和实现，查找遗漏或风险，引用具体文件，最后合并成一份报告。每块都需要独立梳理。不要改文件或运行设备。')
            assert children and running, children
            assert all(row['status'] == 'completed' and row['tool_calls'] > 0 for row in children), children
            checks.append('natural_task_automatically_routes_to_readonly_child')
        if args.start_at != 'background':
            children, running = send('explicit', '启动两个子代理：一个梳理 contract/ 的算子开发契约，一个梳理 recovery/ 的原任务恢复流程。'
                '分别读取各自 README 和 implementation.py，返回 README 第一处冒号前的大写校验码（完整保留下划线，不是普通技术关键词）及实现是否符合契约，引用文件。'
                '必须让各子代理最终报告逐字包含自己读到的校验码，每份报告不超过200字。最后由你汇总，不要修改文件。')
            assert len(children) == 2 and running, children
            assert all(row['status'] == 'completed' and row['tool_calls'] > 0 for row in children), children
            merged = '\n'.join(row['summary'] for row in children)
            assert 'CONTRACT_ORCHID' in merged and 'RECOVERY_CEDAR' in merged, merged
            checks.append('two_explicit_children_read_original_files_and_return_evidence')
            children, _ = send('disabled_by_user', '这次不要启动任何子代理。请你自己读取 recovery/implementation.py，简述缺失 handle 时返回什么。')
            assert not children, children
            checks.append('user_no_delegation_constraint_respected')
        # New evidence was never in the earlier parent conversation. Both child
        # receipts and the parent's final synthesis must contain these markers.
        markers = ['BACKGROUND_CONTRACT_' + uuid.uuid4().hex[:8], 'BACKGROUND_RECOVERY_' + uuid.uuid4().hex[:8]]
        for directory, marker in zip(['contract', 'recovery'], markers):
            name = directory + '/verification.txt'; files[name] = marker + '\n'
            (project / name).write_text(files[name])
        children, running = send('background',
            '后台启动两个只读子代理，分别审查 contract/ 与 recovery/ 的文档和实现，并读取各自新加入的 verification.txt 校验标识。'
            '先给我启动确认，两个后台结果返回后你自行合并成一份简短报告，写出这次读取的两个完整校验标识。'
            '只有两个任务都返回结果后才能输出 BACKGROUND_PAIR_COMPLETE。不要修改文件或运行设备。', background=True)
        assert len(children) == 2 and running and all(row['background'] and row['status'] == 'completed' for row in children), children
        assert phase_facts['background']['foreground_returned_while_children_running'], phase_facts['background']
        assert len(set(phase_facts['background']['notification_ids'])) == 2
        merged = '\n'.join(row['summary'] for row in children)
        assert all(marker in merged for marker in markers), merged
        session_file = max((run / 'state/pi-sessions').glob('*/sessions/*.jsonl'), key=lambda path: path.stat().st_mtime_ns)
        records = [json.loads(line) for line in session_file.read_text().splitlines()]
        answer = next(row['message'] for row in reversed(records) if row.get('type') == 'message' and row['message'].get('role') == 'assistant')
        summary = '\n'.join(block['text'] for block in answer['content'] if block['type'] == 'text')
        assert 'BACKGROUND_PAIR_COMPLETE' in summary and all(marker in summary for marker in markers), summary
        (output / 'background-parent-summary.txt').write_text(summary)
        checks.append('two_live_background_children_notify_once_then_parent_synthesizes_new_evidence')
        ui.send('/agents'); ui.until(lambda: 'kernel-scout' in ui.text() and '本会话（' in ui.text())
        (output / 'subagents-screen.txt').write_text(ui.text())
        os.write(ui.master, b'\x1b'); ui.until(lambda: '本会话（' not in ui.text())
        assert all((project / name).read_text() == text for name, text in files.items())
        ui.send('/quit'); ui.until(lambda: ui.child.poll() is not None); assert ui.child.returncode == 0
        checks.append('native_pi_status_and_child_summaries_no_file_mutation')
        (output / 'scenario.json').write_text(json.dumps({'status': 'passed', 'checks': checks, 'model': client.call('status')['model'],
            'scope': 'real model + local source, no device evaluation'}, ensure_ascii=False, indent=2) + '\n')
        print(json.dumps({'status': 'passed', 'checks': checks}), flush=True)
    finally:
        (output / 'observations.json').write_text(json.dumps(observations, ensure_ascii=False, indent=2) + '\n')
        (output / 'phase-facts.json').write_text(json.dumps(phase_facts, ensure_ascii=False, indent=2) + '\n')
        (output / 'last-screen.txt').write_text(ui.text())
        (output / 'transcript.txt').write_bytes(ui.transcript)
        try: ui.close(output / 'transcript.txt')
        finally:
            if client: client.close()
            cleanup_fixture_hosts(run)


if __name__ == '__main__': main()
