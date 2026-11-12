"""Real Pi/Gateway/business-tools A-I loop using scripted inference, no NPU claims."""
import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'examples/operator_pipeline'))
from pipeline import Pipeline
from product_scenario import cleanup_fixture_hosts


class Provider(BaseHTTPRequestHandler):
    config = None
    project = None
    requests = []
    calls = []
    cycle = 0
    edited = 0
    read_diagnostics = set()
    archived_status = False

    def log_message(self, *_): pass

    def choose(self):
        if not self.calls: return 'pipeline_status', {}
        config_path = self.config / 'config.json'
        pipeline = Pipeline(json.loads(config_path.read_text()))
        state = pipeline.state
        if not state['contract']:
            return 'pipeline_bind', {'contract': {'task': 'local workflow protocol fixture',
                'oracle': {'kind': 'independent', 'description': 'pinned fixture checker, not hardware evidence',
                           'files': {'oracle.py': str(self.project / 'oracle.py')}},
                'measurement': {'scope': 'protocol_fixture'}, 'suite': [{'id': 'tiny', 'family': 'small'}, {'id': 'wide', 'family': 'wide'}]}}
        if not state['baseline']:
            return 'pipeline_baseline', {'selection': 'files', 'files': {'candidate.py': str(self.project / 'baseline.py')}}
        if state.get('archive'):
            if not type(self).archived_status:
                type(self).archived_status = True
                return 'pipeline_status', {}
            return None
        action = pipeline.workflow()['next_actions'][0]
        tool, args = action['tool'], action['arguments'].copy()
        if tool is None: raise AssertionError(action)
        if tool == 'pipeline_plan':
            if args['stage'] == 'C':
                for report in (state.get('diagnostics') or {}).get('reports', []):
                    receipt = Path(report['receipt'])
                    run = receipt.parents[2].name
                    artifact = 'diagnostic-' + report['kind'] + '/driver/' + report['kind'] + '.txt'
                    key = (run, artifact)
                    if key not in self.read_diagnostics:
                        self.read_diagnostics.add(key)
                        return 'pipeline_read', {'run': run, 'path': artifact}
                type(self).cycle += 1
                args['record'] = {'hypothesis': 'fixture variant ' + str(self.cycle), 'diagnostic_artifacts_read': len(self.read_diagnostics)}
            else:
                args['record'] = {'families': ['small', 'wide'], 'per_case_gaps': 'local fixture only'}
        elif tool == 'pipeline_freeze':
            if self.edited != self.cycle:
                type(self).edited = self.cycle
                return 'write', {'path': 'candidate.py', 'content': 'x = ' + str(self.cycle - 1) + '\n'}
            args.update(files={'candidate.py': str(self.project / 'candidate.py')}, origin={'hypothesis': self.cycle, 'scope': 'protocol_fixture'})
        elif tool == 'pipeline_archive':
            args['knowledge'] = {'success': 'fixture third variant', 'failures': ['failed semantics', 'paired regression'], 'scope': 'protocol_fixture'}
        return tool, args

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.requests.append(body)
        call = self.choose()
        if call: self.calls.append(call)
        self.send_response(200); self.send_header('Content-Type', 'text/event-stream'); self.end_headers()
        chunks = [{'choices': [{'index': 0, 'delta': {'role': 'assistant', 'content': 'Executing the configured local workflow.'}}]}]
        if call:
            chunks.append({'choices': [{'index': 0, 'delta': {'tool_calls': [{'index': 0, 'id': 'workflow-' + str(len(self.calls)),
                'type': 'function', 'function': {'name': call[0], 'arguments': json.dumps(call[1])}}]}}]})
        chunks.append({'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'tool_calls' if call else 'stop'}],
                       'usage': {'prompt_tokens': 100, 'completion_tokens': 20}})
        for chunk in chunks:
            self.wfile.write(('data: ' + json.dumps(chunk) + '\n\n').encode()); self.wfile.flush()
        self.wfile.write(b'data: [DONE]\n\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--deployment', type=Path, required=True)
    parser.add_argument('--live-config', type=Path, help='Explicit opt-in real model; drivers remain protocol fixtures')
    args = parser.parse_args(); output = args.output.resolve(); output.mkdir(parents=True, exist_ok=False, mode=0o700)
    project = output / 'project'; project.mkdir()
    (project / 'candidate.py').write_text('x = 0\n'); (project / 'baseline.py').write_text('x = -1\n')
    (project / 'oracle.py').write_text('# Independent local fixture checker, no NPU claim\n')
    contract = {'task': 'local workflow protocol fixture',
        'oracle': {'kind': 'independent', 'description': 'pinned fixture checker, not hardware evidence',
                   'files': {'oracle.py': str(project / 'oracle.py')}},
        'measurement': {'scope': 'protocol_fixture'}, 'suite': [{'id': 'tiny', 'family': 'small'}, {'id': 'wide', 'family': 'wide'}]}
    (project / 'workflow-inputs.json').write_text(json.dumps({'contract': contract,
        'baseline_files': {'candidate.py': str(project / 'baseline.py')}, 'candidate_files': {'candidate.py': str(project / 'candidate.py')},
        'variant_contract': 'candidate.py must contain exactly x = <integer>. 0 is the initial incorrect variant; 1 is correct but slow; 2 is correct and fast.',
        'scope': 'This exercises orchestration, NOT NPU speed. Driver latencies and diagnostic artifacts are deterministic fixtures.'}, indent=2))
    (project / 'AGENTS.md').write_text('Only edit candidate.py. Read workflow-inputs.json for the task contract and file mappings. '
        'Preserve baseline.py/oracle.py. This is an explicitly authorized local workflow fixture. '
        'Only pipeline tools execute the pinned checks/measurements. Do not change drivers, contract, baseline or oracle. '
        'Never call this an official result or device performance.\n')
    driver = ROOT / 'validation/tui/operator-workflow-driver.py'
    drivers = {kind: {'kind': 'diagnostic' if kind in {'ir', 'profiler'} else kind,
                     'argv': [sys.executable, '-B', str(driver), kind], 'files': [str(driver)], 'timeout': 60}
               for kind in ['launch', 'collect', 'check', 'measure', 'calibrate', 'ir', 'profiler']}
    server = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    config = {'providers': {'primary': {'model': 'workflow-fixture', 'base_url': f'http://127.0.0.1:{server.server_port}/v1',
               'api_key_env': 'KERNELBRIDGE_TEST_KEY', 'allow_insecure_http': True}}, 'subagents': {'enabled': False},
              'operator_pipeline': {'read_roots': [str(project)], 'drivers': drivers,
                  'diagnostics': {'on_failure': True, 'on_regression': True, 'required': False,
                                  'scope': 'protocol_fixture', 'drivers': {'ir': 'ir', 'profiler': 'profiler'}}}}
    if args.live_config:
        live = json.loads(args.live_config.read_text())
        config = {**live, 'subagents': {'enabled': False}, 'operator_pipeline': config['operator_pipeline']}
        config.pop('operator_infra', None); config.pop('business_tools', None)
    descriptor = os.open(output / 'model.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w') as stream: json.dump(config, stream)
    host = output / 'host'
    options = {'cwd': str(project), 'config': str(output / 'model.json'), 'output': str(host), 'python': sys.executable,
               'expectedPythonModules': str(args.deployment.resolve() / 'expected.json')}
    if args.live_config:
        options['prompt'] = ('请读取 workflow-inputs.json，完成这个本地算子工作流夹具的优化与归档。先在完全不修改 candidate.py 的情况下，'
            '建立当前实现的检查记录；根据返回的错误及诊断产物，再修改实现直到全部 case 正确且完整配对比给定 baseline 快，保存成功机制和失败条件。'
            '通过后归档并查看下一轮状态，然后停止。你自己调用已配置的工具推进全过程，不要只给我操作步骤。'
            '这是编排验收：所有测量和诊断均为协议夹具，不是设备性能或官方验收，不要外部提交。')
    (output / 'options.json').write_text(json.dumps(options))
    Provider.config = host / 'inputs/operator_pipeline'; Provider.project = project
    env = {**os.environ, 'PYTHONPATH': str(args.deployment.resolve() / 'unpacked'), 'KERNELBRIDGE_TEST_KEY': 'fixture-only',
           'KERNELBRIDGE_HOME': str(output / 'home'), 'KERNELBRIDGE_SESSIONS': str(Path('/tmp').resolve() / ('kb-workflow-' + hashlib.sha256(str(output).encode()).hexdigest()[:8]))}
    if args.live_config: env.pop('KERNELBRIDGE_HOME', None)
    try:
        result = subprocess.run([shutil.which('node'), str(ROOT / 'validation/tui/operator-workflow-scenario.mjs'), str(output / 'options.json')],
                                env=env, capture_output=True, text=True, timeout=600)
        (output / 'stdout.log').write_text(result.stdout); (output / 'stderr.log').write_text(result.stderr)
        (output / 'model-requests.json').write_text(json.dumps(Provider.requests, indent=2))
        (output / 'calls.json').write_text(json.dumps(Provider.calls, indent=2))
        if result.returncode: raise AssertionError(result.stderr)
        if not args.live_config:
            prompt = json.dumps(Provider.requests[0]['messages'])
            assert 'execute the workflow with the available tools' in prompt
        pipeline = Pipeline(json.loads((Provider.config / 'config.json').read_text()))
        assert pipeline.state['archive'] and pipeline.state['calibration']
        assert pipeline.state['check']['passed'] and pipeline.state['pair']['speedup'] > 1
        states = [json.loads(path.read_text())['body'] for path in sorted(Path(pipeline.config['state']).glob('event-*.json'))]
        assert set('ABCDEFGHI') <= {state['stage'] for state in states}
        failure_index = next(i for i, state in enumerate(states) if state.get('diagnostic_runs'))
        assert any(state['stage'] == 'C' for state in states[failure_index + 1:]), 'Diagnostics must feed a new hypothesis'
        assert all(row.get('collected') for row in pipeline.state['handles'])
        assert len(pipeline.state['handles']) >= (2 if args.live_config else 3)
        assert len(pipeline.state['diagnostic_runs']) >= (2 if args.live_config else 4)
        journal = [json.loads(line) for line in (host / 'state/events.jsonl').read_text().splitlines()]
        artifact_reads = {row['action']['id'] for row in journal if row.get('kind') == 'admitted'
                          and row.get('action', {}).get('tool') == 'pipeline_read'
                          and row['action']['arguments'].get('path', '').startswith('diagnostic-')}
        completed_reads = [row for row in journal if row.get('kind') == 'settled' and row.get('action_id') in artifact_reads
                           and row['outcome']['status'] == 'completed']
        assert len(completed_reads) >= (1 if args.live_config else 4), 'Harness must actually read diagnostic artifacts'
        reads = [json.loads((Path(pipeline.config['state']) / row['run'] / 'driver/request.json').read_text())
                 for row in pipeline.state['receipts'] if (Path(pipeline.config['state']) / row['run'] / 'driver/request.json').exists()]
        assert any(row['role'] == 'F' for row in reads)
        if not args.live_config:
            assert sum(name == 'pipeline_read' for name, _ in Provider.calls) == 4
            assert Provider.cycle == 3
        assert pipeline.workflow()['next_actions'][0]['stage'] == 'B'
        summary = {'status': 'passed', 'scope': 'real Pi and Gateway, ' + ('real model' if args.live_config else 'scripted model') + ', protocol-fixture driver; no NPU or official evidence',
                   'checks': ['A_to_I_via_harness_tools', 'failure_J_to_C',
                              'source_bound_artifact_reads', 'original_handle_collection', 'I_to_B_next_cycle', 'no_implicit_budget'],
                   'model_calls': json.loads((host / 'final-state.json').read_text())['model_turns'],
                   'pipeline_tool_calls': len(pipeline.state['receipts'])}
        if not args.live_config: summary['checks'] += ['conditional_workflow_prompt', 'regression_J_to_C']
        (output / 'scenario.json').write_text(json.dumps(summary, indent=2) + '\n'); print(json.dumps(summary))
    finally:
        cleanup_fixture_hosts(host); server.shutdown()


if __name__ == '__main__': main()
