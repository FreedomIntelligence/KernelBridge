"""Opt-in official DeepSeek PTY check; uses the user's private configuration."""
import argparse
import json
import os
from pathlib import Path
import socket
import time
import uuid

from codegen_edges import Terminal
from product_scenario import cleanup_fixture_hosts
from pty_scenario import Client


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--launcher', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    launcher = str(args.launcher.resolve())
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    project = output / 'project'
    project.mkdir()
    (project / 'solution.py').write_text('x = 1\n')
    run = output / 'run'
    env = {**os.environ, 'TERM': 'xterm-256color',
           'KERNELBRIDGE_SESSIONS': str(Path('/tmp').resolve() / ('kb-ds-' + uuid.uuid4().hex[:10]))}
    # Prove that fresh-shell startup and restart load only the private user file.
    for name in ['DEEPSEEK_API_KEY', 'KERNELBRIDGE_HOME', 'KERNELBRIDGE_CONFIG']:
        env.pop(name, None)
    checks = []
    states = []
    for phase in ['initial', 'resume']:
        if phase == 'initial':
            command = [launcher, '--output', str(run)]
        else:
            binding = json.loads((run / 'application.json').read_text())
            command = [launcher, 'resume', binding['application_session_id']]
        ui = Terminal(command, project, env)
        client = None
        try:
            ui.until(lambda: 'deepseek-flash' in ui.text() and '/help · /latest' in ui.text())
            binding = json.loads((run / 'application.json').read_text())
            client = Client(Path(binding['host_directory']))
            state = client.call('status')
            assert 'v1.5.0' in ui.text() and 'controlled' not in ui.text()
            assert 'Trial' not in ui.text() and '预算' not in ui.text()
            if phase == 'initial':
                assert state['model_turns'] == 0 and not state['inputs'] and state['budget']['spent'] == 0
                prompt = ('Use read to inspect solution.py. Use edit to change x = 1 to x = 2. '
                          'Then use bash to run python3 -c "from solution import x; assert x == 2; '
                          "print('DS_LIVE_OK')\". Do not inspect other files. Finish with DS_LIVE_OK.")
                checks.append('native_pi_idle_private_credentials_zero_execution')
            else:
                assert state['model_turns'] == states[0]['model_turns']
                assert state['host_pid'] != states[0]['host_pid']
                assert (project / 'solution.py').read_text() == 'x = 2\n'
                prompt = 'Reply exactly DS_RESUME_OK without using tools.'
                checks.append('host_restart_private_credentials_no_replay')
            (output / f'{phase}-idle-screen.txt').write_text(ui.text())
            ui.send(prompt)
            ui.until(lambda: any(i['text'] == prompt and i['status'] in {'completed', 'failed', 'interrupted'}
                                for i in client.call('status')['inputs'].values()), timeout=180)
            state = client.call('status')
            receipt = next(i for i in state['inputs'].values() if i['text'] == prompt)
            assert receipt['status'] == 'completed', receipt
            ui.until(lambda: '等待输入' in ui.text() and ('DS_LIVE_OK' if phase == 'initial' else 'DS_RESUME_OK') in ui.text())
            assert (project / 'solution.py').read_text() == 'x = 2\n'
            assert not state['pending_effect_ids'] and state['goal'] is None and not state['trials']
            assert state['budget']['limit'] is None and state['budget']['reserved'] == 0
            if phase == 'resume':
                assert state['model_turns'] == states[0]['model_turns'] + 1
            (output / f'{phase}-completed-screen.txt').write_text(ui.text())
            checks.append('live_read_edit_bash_returns_idle' if phase == 'initial' else 'live_resumed_response')
            if phase == 'initial':
                ui.send('/model ds-pro')
                ui.until(lambda: 'deepseek-v4-pro' in ui.text())
                assert client.call('status')['model'] == 'deepseek-v4-pro'
                ui.send('/model ds')
                ui.until(lambda: 'deepseek-flash' in ui.text())
                assert client.call('status')['model'] == 'deepseek-flash'
                checks.append('model_route_switch')
            states.append(state)
            (output / f'{phase}-state.json').write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\n')
            ui.send('/quit')
            ui.until(lambda: ui.child.poll() is not None)
            assert ui.child.returncode == 0
        finally:
            try:
                ui.close(output / f'{phase}-transcript.txt')
            finally:
                if client:
                    client.close()
                cleanup_fixture_hosts(run)
        # SIGTERM may leave a stale socket; restart must reclaim it normally.
        deadline = time.monotonic() + 10
        while True:
            try:
                with socket.socket(socket.AF_UNIX) as connection:
                    connection.settimeout(.5)
                    connection.connect(str(Path(binding['host_directory'], 'host.sock')))
            except (FileNotFoundError, ConnectionRefusedError):
                break
            if time.monotonic() >= deadline:
                raise AssertionError('Fixture host did not stop')
            time.sleep(.1)
    result = {'status': 'passed', 'provider': 'DeepSeek official', 'model': 'deepseek-flash',
              'live_model_turns': states[-1]['model_turns'], 'checks': checks, 'states': states}
    (output / 'scenario.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({k: v for k, v in result.items() if k != 'states'}), flush=True)


if __name__ == '__main__':
    main()
