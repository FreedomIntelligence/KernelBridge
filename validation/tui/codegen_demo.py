"""Idle-first codegen demo using a local deterministic provider."""
import argparse
import hashlib
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import threading
from uuid import uuid4

from codegen_fixture import CodegenProvider
from product_scenario import ROOT, cleanup_fixture_hosts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--launcher', type=Path, default=ROOT / 'kernelagent')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--resume', action='store_true')
    args = parser.parse_args()
    if not sys.stdin.isatty() or not sys.stdout.isatty(): parser.error('run this demo in an interactive terminal')
    launcher = args.launcher.resolve()
    source = Path('source/apps/agent/src/application/codegen.mjs')
    if not (launcher.parent / source).exists() or (launcher.parent / source).read_bytes() != (ROOT / source).read_bytes():
        parser.error(f'所选入口不是当前 codegen 版本。请使用 --launcher {ROOT / "kernelagent"}')
    if args.resume and not args.output: parser.error('--resume requires the original --output')
    output = (args.output or ROOT / '.validation' / ('manual-codegen-' + uuid4().hex[:12])).resolve()
    if not args.resume:
        output.mkdir(parents=True, exist_ok=False)
        (output / 'project').mkdir()
        (output / 'project/solution.py').write_text('x = 1\n')
        port = 0
    else:
        descriptor = json.loads((output / 'demo.json').read_text())
        if descriptor['launcher'] != str(launcher): parser.error('resume with the original launcher')
        port = descriptor['port']
        CodegenProvider.requests = json.loads((output / 'model-requests.json').read_text())
        for request in CodegenProvider.requests: CodegenProvider.turns[request['model']] += 1
    with ThreadingHTTPServer(('127.0.0.1', port), CodegenProvider) as provider:
        if not args.resume:
            (output / 'model.json').write_text(json.dumps({'providers': {'primary': {
                'model': 'local-codegen-demo', 'base_url': f'http://127.0.0.1:{provider.server_port}/v1',
                'api_key_env': 'KERNELBRIDGE_TEST_KEY'}}}))
            (output / 'demo.json').write_text(json.dumps({'launcher': str(launcher), 'port': provider.server_port, 'mode': 'codegen'}))
        registry = Path('/tmp').resolve() / ('kb-demo-' + hashlib.sha256(str(output).encode()).hexdigest()[:10])
        environment = {**os.environ, 'PI_OFFLINE': '1', 'KERNELBRIDGE_DEMO': '1',
            'KERNELBRIDGE_TEST_KEY': 'fixture-not-secret', 'KERNELBRIDGE_SESSIONS': str(registry),
            'KERNELBRIDGE_HOME': str(output / 'home'), 'KERNELBRIDGE_CONFIG': str(output / 'model.json')}
        thread = threading.Thread(target=provider.serve_forever, daemon=True); thread.start()
        print('本地固定脚本演示，无真实模型或 NPU。进入后等待输入。\n'
              '可输入：读取 solution.py，把 x = 1 改成 x = 2，再用 bash 检查。\n'
              f'演示目录：{output}', flush=True)
        try:
            return subprocess.run([str(launcher), *(['--continue'] if args.resume else [])],
                cwd=output / 'project', env=environment).returncode
        finally:
            for run in (output / 'home').glob('runs/*'): cleanup_fixture_hosts(run)
            provider.shutdown(); thread.join(timeout=5)
            (output / 'model-requests.json').write_text(json.dumps(CodegenProvider.requests, indent=2))
            if list((output / 'home').glob('projects/*.json')):
                print('恢复演示：\n' + shlex.join([sys.executable, '-B', str(Path(__file__).resolve()),
                    '--launcher', str(launcher), '--output', str(output), '--resume']), flush=True)


if __name__ == '__main__': raise SystemExit(main())
