"""Opt-in real-model delegation routing, foreground children and visible Pi status."""
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
    client = None; checks = []; observations = []

    def send(label, prompt):
        before = client.call('status'); ui.send(prompt)
        deadline = time.monotonic() + 360
        running_seen = False
        while time.monotonic() < deadline:
            ui.pump()
            state = client.call('status')
            if state['subagents'] != (observations[-1]['subagents'] if observations else None):
                observations.append({'phase': label, 'subagents': state['subagents']})
            if any(row['status'] == 'running' for row in state['subagents']):
                running_seen = True
                if '子代理' in ui.text(): (output / (label + '-running-screen.txt')).write_text(ui.text())
            receipt = next((v for k, v in state['inputs'].items() if k not in before['inputs'] and v['text'] == prompt), None)
            if receipt and receipt['status'] in {'completed', 'failed', 'interrupted'}:
                assert receipt['status'] == 'completed', receipt
                break
        else: raise AssertionError('Live prompt did not settle; original receipts retained')
        ui.until(lambda: '等待输入' in ui.text())
        assert not state['pending_effect_ids'] and state['budget']['reserved'] == 0
        assert state['budget']['limit'] is None and not state['goal'] and not state['trials']
        (output / (label + '-state.json')).write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\n')
        (output / (label + '-screen.txt')).write_text(ui.text())
        return state['subagents'][len(before['subagents']):], running_seen

    try:
        ui.until(lambda: '等待输入' in ui.text() and '上下文剩余' in ui.text())
        binding = json.loads((run / 'application.json').read_text()); client = Client(Path(binding['host_directory']))
        assert client.call('status')['model_turns'] == 0
        children, _ = send('simple', '读取 README.md，只说项目包含哪两个子目录。')
        assert not children, children
        checks.append('simple_lookup_stays_in_parent')
        children, running = send('automatic', '请完整审查两个相互独立的子系统 contract/ 与 recovery/：分别核对文档契约和实现，查找遗漏或风险，引用具体文件，最后合并成一份报告。每块都需要独立梳理。不要改文件或运行设备。')
        assert children and running, children
        assert all(row['status'] == 'completed' and row['tool_calls'] > 0 for row in children), children
        checks.append('natural_task_automatically_routes_to_readonly_child')
        children, running = send('explicit', '启动两个子代理：一个梳理 contract/ 的算子开发契约，一个梳理 recovery/ 的原任务恢复流程。分别返回 README 中的标识词和实现是否符合契约，引用文件。最后由你汇总，不要修改文件。')
        assert len(children) == 2 and running, children
        assert all(row['status'] == 'completed' and row['tool_calls'] > 0 for row in children), children
        merged = '\n'.join(row['summary'] for row in children)
        assert 'CONTRACT_ORCHID' in merged and 'RECOVERY_CEDAR' in merged, merged
        checks.append('two_explicit_children_read_original_files_and_return_evidence')
        children, _ = send('disabled_by_user', '这次不要启动任何子代理。请你自己读取 recovery/implementation.py，简述缺失 handle 时返回什么。')
        assert not children, children
        checks.append('user_no_delegation_constraint_respected')
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
        (output / 'last-screen.txt').write_text(ui.text())
        ui.close(output / 'transcript.txt')
        if client: client.close()
        cleanup_fixture_hosts(run)


if __name__ == '__main__': main()
