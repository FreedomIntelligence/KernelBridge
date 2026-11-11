"""Local operator diagnostics: collection, parsing, bounded reads and IR rendering."""
import csv
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
from functools import partial


def sha(data):
    return hashlib.sha256(data).hexdigest()


def inventory(root):
    files = []
    for path in sorted(root.rglob('*')):
        if path.is_symlink():
            continue
        if path.is_file() and path.name != 'manifest.json':
            files.append({'path': path.relative_to(root).as_posix(), 'bytes': path.stat().st_size,
                          'sha256': sha(path.read_bytes())})
    return files


def artifact(root, collection, name=None):
    if not re.fullmatch(r'[a-f0-9]{64}', collection):
        raise ValueError('Expected a collection identity returned by a diagnostic tool')
    folder = root / collection
    if folder.resolve() != folder:
        raise ValueError('Diagnostic storage cannot traverse links')
    manifest = json.loads((folder / 'manifest.json').read_text())
    if name is None:
        return manifest
    path = folder / name
    row = next((row for row in manifest['files'] if row['path'] == name), None)
    if not row or path.resolve() != path or not path.is_relative_to(folder) or '..' in Path(name).parts:
        raise ValueError('Artifact is not in this collection')
    if path.stat().st_size > 128 * 1024 * 1024:
        raise ValueError('Artifact exceeds diagnostic read limit')
    if sha(path.read_bytes()) != row['sha256']:
        raise ValueError('Diagnostic artifact digest changed')
    return path


def emit_device_bitcode(build, output, compiler):
    """Replay retained device compilation with the same source, defines and target."""
    records = []
    seen = set()
    for log in sorted(build.rglob('*.log')):
        for line in log.read_text(errors='replace').splitlines():
            if not line.startswith('/') or 'bisheng ' not in line:
                continue
            argv = shlex.split(line)
            if Path(argv[0]).resolve() != Path(compiler).resolve() or '-c' not in argv or '-E' in argv:
                continue
            if not any(arg.startswith('--cce-aicore-arch=') for arg in argv) or '-D__GET_CODE_CHANNEL__' in argv:
                continue
            sources = [Path(arg) for arg in argv if arg.endswith(('.cpp', '.cce'))]
            if not sources or any(not p.resolve().is_relative_to(build.resolve()) for p in sources):
                continue
            key = tuple(argv)
            if key in seen:
                continue
            seen.add(key)
            directory = output / str(len(records)); directory.mkdir(parents=True)
            command = []; index = 0
            while index < len(argv):
                arg = argv[index]; index += 1
                if arg in ('-o', '-MF', '-MT'):
                    index += 1
                elif arg not in ('-MD', '-MMD', '-save-temps'):
                    command.append(arg)
            command += ['-emit-llvm', '-o', str(directory / 'kernel.bc')]
            with (directory / 'compile.log').open('w') as stream:
                process = subprocess.run(command, cwd=directory, stdout=stream, stderr=subprocess.STDOUT, timeout=180)
            record = {'original_command': argv, 'command': command, 'returncode': process.returncode,
                      'sources': [{'path': str(p.relative_to(build)), 'sha256': sha(p.read_bytes())} for p in sources]}
            records.append(record)
    (output / 'commands.json').write_text(json.dumps(records, indent=2) + '\n')
    return list(output.rglob('*.bc'))


def collect(config, root, kind):
    from kernelcat_executor.build.inputs import BuildSettings
    from kernelcat_executor.backends.ascend.compiler import compile_sources
    from kernelcat_executor.evaluation.protocols.six_source.session import AscendBackend
    from kernelcat_executor.evaluation.protocols.six_source.config import BACKEND_ROOT
    source = Path.cwd() / 'solution.py'
    if source.resolve() != source or not source.is_file() or source.stat().st_size > 4_000_000:
        raise ValueError('Expected the admitted solution.py')
    candidate = source.read_bytes()
    (root / 'solution.py').write_bytes(candidate)
    build = root / 'build'; build.mkdir()
    shutil.copytree(BACKEND_ROOT / 'templates/CppExtension', build / 'CppExtension')
    settings = BuildSettings(config['operator'], build, config['soc'], config['device'])
    os.environ.update(ASCEND_DEVICE_ID=str(config['device']), ASCENDC_DEVICE=config['soc'],
                      OP_ENGINEER_DIR=str(build), PYTHON=sys.executable)
    compiler = partial(compile_sources, diagnostic_options=('-save-temps', '--save-temp-files') if kind == 'ir' else ())
    backend = AscendBackend(settings, compiler=compiler, atol=config['atol'], rtol=config['rtol'])
    try:
        ok, error = backend.compile(candidate.decode())
        if not ok:
            raise RuntimeError(error)
        result = {'candidate_sha256': sha(candidate), 'build': backend.build_manifest, 'kind': kind,
                  'device': config['device'], 'soc': config['soc']}
        if kind == 'ir':
            compiler_path = shutil.which('bisheng')
            if not compiler_path:
                home = Path(os.environ.get('ASCEND_HOME_PATH', '/usr/local/Ascend/ascend-toolkit/latest'))
                matches = sorted(home.glob('**/bin/bisheng'))
                compiler_path = str(matches[0]) if matches else None
            if not compiler_path:
                raise ValueError('bisheng is required for LLVM decoding')
            result['compiler_sha256'] = sha(Path(compiler_path).read_bytes())
            result['compiler_version'] = subprocess.check_output([compiler_path, '--version'], text=True, timeout=20)
            bitcode = list(build.rglob('*.bc'))
            decoded = root / 'llvm'; decoded.mkdir()
            if not bitcode:
                bitcode = emit_device_bitcode(build, decoded, compiler_path)
            for index, path in enumerate(bitcode):
                target = decoded / (str(index) + '-' + path.stem + '.ll')
                command = [compiler_path, '-cc1', '-x', 'ir', '-emit-llvm', '-triple', 'hiipu64-hisilicon-cce', '-o', str(target), str(path)]
                with (decoded / (target.stem + '.log')).open('w') as log:
                    process = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, timeout=120)
                if process.returncode:
                    target.unlink(missing_ok=True)
            result['llvm_files'] = [str(path.relative_to(root)) for path in decoded.glob('*.ll')]
            if not result['llvm_files']:
                raise ValueError('Compiler retained no decodable device bitcode; inspect build logs')
        else:
            import torch
            import torch_npu
            from kernelcat_executor.evaluation.protocols.six_source.correctness import _to_device
            reference = Path(config['reference']).read_text()
            correct, info, stage = backend.correctness(reference)
            result.update(correctness=correct, correctness_info=info, failure_stage=stage)
            if not correct:
                raise ValueError('Diagnostic candidate failed correctness: ' + str(info))
            context = backend.context
            model = context['ModelNew'](*_to_device(context['get_init_inputs'](), backend.runtime.device)).to(backend.runtime.device)
            inputs = _to_device(context['get_inputs'](), backend.runtime.device)
            with torch.no_grad():
                for _ in range(3): model(*inputs)
                torch_npu.npu.synchronize()
                experimental = torch_npu.profiler._ExperimentalConfig(
                    profiler_level=torch_npu.profiler.ProfilerLevel.Level1,
                    aic_metrics=torch_npu.profiler.AiCMetrics.PipeUtilization)
                with torch_npu.profiler.profile(activities=[torch_npu.profiler.ProfilerActivity.NPU],
                    on_trace_ready=torch_npu.profiler.tensorboard_trace_handler(str(root / 'profile')),
                    record_shapes=False, with_stack=False, profile_memory=False, experimental_config=experimental):
                    for _ in range(5):
                        model(*inputs); torch_npu.npu.synchronize()
            result.update(torch=torch.__version__, torch_npu=torch_npu.__version__,
                          metric='PipeUtilization', warmup=3, repeats=5, measurement_kind='profiled_diagnostic')
            if not list((root / 'profile').rglob('kernel_details.csv')):
                raise ValueError('Profiler returned no kernel_details.csv')
        return result
    finally:
        backend.close()


def main(config):
    action = json.load(sys.stdin); name = action['tool']; args = action['arguments']
    root = Path(config['state']) / 'diagnostics'; root.mkdir(parents=True, exist_ok=True)
    if name == 'diagnostics_read':
        item = artifact(root, args['collection'], args.get('path'))
        if isinstance(item, dict):
            return {'status': 'completed', 'data': item}
        offset = args.get('offset', 0); limit = min(args.get('limit', 8192), 16384)
        with item.open('rb') as stream:
            stream.seek(offset); data = stream.read(limit)
        return {'status': 'completed', 'data': {'text': data.decode('utf-8', errors='replace'),
                'offset': offset, 'next_offset': offset + len(data), 'bytes': item.stat().st_size}}
    identity = sha(action['id'].encode()); output = root / identity; output.mkdir()
    try:
        if name in ('ir_collect', 'profiler_collect'):
            # Native/compiler output is retained separately from the finite JSON reply.
            with (output / 'collector.log').open('w') as log:
                original = os.dup(1); os.dup2(log.fileno(), 1)
                try: report = collect(config, output, 'ir' if name == 'ir_collect' else 'profiler')
                finally: sys.stdout.flush(); os.dup2(original, 1); os.close(original)
        elif name == 'diagnostics_parse':
            path = artifact(root, args['collection'], args['path'])
            if path.suffix == '.ll':
                from ir_bridge import llvm_stats
                report = {'kind': 'llvm', **llvm_stats(path.read_text()), 'artifact_sha256': sha(path.read_bytes()),
                          'collection': args['collection'], 'source_identity': artifact(root, args['collection']).get('report', {'binding': 'unavailable'})}
            elif path.suffix == '.csv':
                with path.open(newline='') as stream: rows = list(csv.DictReader(stream))
                report = {'kind': 'profiler_csv', 'row_count': len(rows), 'columns': list(rows[0]) if rows else [],
                          'rows': rows[:32], 'artifact_sha256': sha(path.read_bytes()), 'measurement_kind': 'profiled_diagnostic',
                          'collection': args['collection'], 'source_identity': artifact(root, args['collection']).get('report', {'binding': 'unavailable'})}
            else:
                raise ValueError('Select a .ll or profiler .csv artifact')
        elif name == 'diagnostics_render':
            from ir_bridge import render_ir
            paths = [artifact(root, args[prefix + '_collection'], args[prefix + '_path']) for prefix in ('before', 'after')]
            report = render_ir(paths, output / 'ir.html', 'llvm')
        else:
            raise ValueError('Unknown diagnostic operation')
        status = 'completed'
    except Exception as error:
        status = 'failed'; report = {'error': str(error)}
    (output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    manifest = {'collection': identity, 'operation': name, 'status': status, 'report': report, 'files': inventory(output)}
    (output / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    return {'status': status, 'data': {'collection': identity, 'report': report,
            'files': [row for row in manifest['files'] if not row['path'].startswith('build/')][:128]}}
