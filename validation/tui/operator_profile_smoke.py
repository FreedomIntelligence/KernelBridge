"""Open an explicitly configured native operator TUI without model/device calls."""
import argparse
import json
import os
from pathlib import Path
import uuid

from codegen_edges import Terminal
from product_scenario import ROOT, cleanup_fixture_hosts
from pty_scenario import Client


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args(); output = args.output.resolve(); output.mkdir(exist_ok=False)
    project = output / 'project'; project.mkdir()
    config = json.loads(args.config.read_text())
    environment = {**os.environ, 'TERM': 'xterm-256color', 'KERNELBRIDGE_HOME': str(output / 'home'),
        'KERNELBRIDGE_SESSIONS': str(Path('/tmp').resolve() / ('kb-profile-' + uuid.uuid4().hex[:8]))}
    credentials_path = Path.home() / '.kernelbridge/credentials.json'
    credentials = json.loads(credentials_path.read_text()) if credentials_path.exists() else {}
    for provider in config['providers'].values():
        key = provider['api_key_env']
        if not environment.get(key) and key in credentials: environment[key] = credentials[key]
    ui = Terminal([str(ROOT / 'kernelagent'), '--config', str(args.config.resolve())], project, environment)
    client = None
    try:
        ui.until(lambda: '等待输入' in ui.text())
        run = next((output / 'home/runs').iterdir()); binding = json.loads((run / 'application.json').read_text())
        client = Client(Path(binding['host_directory'])); state = client.call('status')
        frozen = json.loads((run / 'config.json').read_text())
        assert state['budget']['spent'] == 0 and state['goal'] is None and not state['trials']
        assert len(frozen['business_tools']) == 27
        assert {'pipeline_bind', 'pipeline_baseline', 'infra_context', 'infra_trace_pc'} <= {t['name'] for t in frozen['business_tools']}
        ui.send('/subagents')
        ui.until(lambda: any(x['text'] == '/subagents' and x['status'] == 'completed' for x in client.call('status')['inputs'].values()))
        ui.send('/quit'); ui.until(lambda: ui.child.poll() is not None); assert ui.child.returncode == 0
        result = {'status': 'passed', 'model': state['model'], 'registered_operator_tools': 27, 'model_calls': 0, 'device_calls': 0}
        (output / 'scenario.json').write_text(json.dumps(result, indent=2) + '\n'); print(json.dumps(result))
    finally:
        for run in (output / 'home/runs').glob('*'): cleanup_fixture_hosts(run)
        ui.close(output / 'transcript.txt')
        if client: client.close()


if __name__ == '__main__': main()
