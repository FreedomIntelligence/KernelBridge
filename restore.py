"""Install pinned host dependencies, restore a Pi archive, and optionally continue its Goal."""
from __future__ import annotations
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parent
NODE_VERSION = '22.22.3'


def save(path, value):
    with path.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    path.chmod(0o600)


def run(argv, *, cwd=None, log=None, env=None):
    result = subprocess.run(list(map(str, argv)), cwd=cwd, env=env, text=True,
                            stdout=log, stderr=subprocess.STDOUT if log else None, check=True)
    return result


def node_valid(path):
    try:
        result = subprocess.check_output([str(path), '-e',
            "console.log(JSON.stringify({v:process.versions.node,z:typeof require('node:zlib').createZstdDecompress==='function'}))"],
            text=True, timeout=20)
        data = json.loads(result)
        return tuple(map(int, data['v'].split('.'))) >= (22, 22, 3) and data['z']
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def download(url, destination):
    with urllib.request.urlopen(url, timeout=90) as response, destination.open('xb') as output:
        shutil.copyfileobj(response, output)


def choose_node(explicit, cache):
    if explicit:
        selected = Path(explicit).absolute()
        if not node_valid(selected):
            raise ValueError('--node requires Node 22.22.3+ with Zstd support')
        return selected
    existing = shutil.which('node')
    if existing and node_valid(existing):
        return Path(existing).absolute()
    system = {'Darwin': 'darwin', 'Linux': 'linux'}.get(platform.system())
    arch = {'aarch64': 'arm64', 'arm64': 'arm64', 'x86_64': 'x64', 'AMD64': 'x64'}.get(platform.machine())
    if not system or not arch:
        raise ValueError('Automatic Node installation supports macOS/Linux arm64/x64; pass --node for this host')
    name = f'node-v{NODE_VERSION}-{system}-{arch}'
    toolchains = cache / 'toolchains'
    toolchains.mkdir(parents=True, exist_ok=True)
    target = toolchains / name
    if node_valid(target / 'bin/node'):
        return target / 'bin/node'
    stage = toolchains / ('download-' + uuid.uuid4().hex)
    stage.mkdir()
    archive = stage / (name + '.tar.gz')
    base = f'https://nodejs.org/dist/v{NODE_VERSION}/'
    download(base + 'SHASUMS256.txt', stage / 'SHASUMS256.txt')
    expected = next((line.split()[0] for line in (stage / 'SHASUMS256.txt').read_text().splitlines()
                     if line.split()[-1] == archive.name), None)
    if expected is None:
        raise ValueError('Node distribution has no checksum for this platform')
    download(base + archive.name, archive)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != expected:
        raise ValueError('Node archive checksum mismatch')
    with tarfile.open(archive) as package:
        for member in package.getmembers():
            path = stage / member.name
            if not path.resolve().is_relative_to(stage) or member.isdev():
                raise ValueError('Unsafe Node archive entry')
            if member.issym() or member.islnk():
                link = path.parent / member.linkname if member.issym() else stage / member.linkname
                if not link.resolve().is_relative_to(stage):
                    raise ValueError('Unsafe Node archive link')
        package.extractall(stage, filter='data')
    (stage / name).rename(target)
    if not node_valid(target / 'bin/node'):
        raise ValueError('Downloaded Node cannot execute on this host')
    save(target / 'kernelbridge-toolchain.json', {'url': base + archive.name, 'sha256': expected})
    return target / 'bin/node'


def installed_copy(cache, node, python):
    spec = importlib.util.spec_from_file_location('kb_installer', ROOT / 'install.py')
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    installer.verify_bundle(ROOT)
    pin = hashlib.sha256((ROOT / 'PIN_MANIFEST.json').read_bytes()).hexdigest()
    identity = hashlib.sha256((pin + str(python) + str(node) + platform.platform()).encode()).hexdigest()[:24]
    bucket = cache / 'installations' / identity
    bucket.mkdir(parents=True, exist_ok=True)
    ready = bucket / 'ready.json'
    if ready.is_file():
        record = json.loads(ready.read_text())
        bundle = Path(record['bundle'])
        installer.verify_bundle(bundle)
        runtime = bundle / 'runtime'
        if not (runtime / 'venv/bin/loopx').is_file() or not (bundle / '.installation/ready').is_file():
            raise ValueError('Cached installation is incomplete; select another --cache directory')
        check = [runtime / 'venv/bin/python', '-I', '-B', '-c',
                 'import kernelcat_worker, kernelcat_executor, kernelcat_ingestion, kernelcat_knowledge']
        run(check)
        return bundle, runtime, record
    bundle = bucket / ('bundle-' + uuid.uuid4().hex[:12])
    bundle.mkdir()
    for name in [*installer.manifest_files(ROOT), 'PIN_MANIFEST.json']:
        target = bundle / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)
    runtime = bundle / 'runtime'
    npm = node.parent / 'npm'
    if not npm.is_file():
        raise ValueError('Selected Node installation must include npm in the same bin directory')
    environment = installer.installation_environment(node)
    with (bundle / 'bootstrap.log').open('x') as log:
        run([python, '-I', '-B', bundle / 'install.py', '--runtime', runtime,
             '--python', python, '--node', node, '--npm', npm], log=log, env=environment)
        run([runtime / 'venv/bin/python', '-I', '-B', '-m', 'pip', 'install', '--no-deps', '--no-compile',
             '--require-hashes', '-r', bundle / 'source/apps/agent/loopx-requirements.txt'], log=log, env=environment)
        run([runtime / 'venv/bin/python', '-I', '-B', '-m', 'pip', 'check'], log=log, env=environment)
    record = {'bundle': str(bundle), 'runtime': str(runtime), 'node': str(node), 'python': str(python),
              'source_manifest_sha256': pin, 'loopx': '1.2.3', 'platform': platform.platform()}
    save(ready, record)
    return bundle, runtime, record


def main():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('--run', type=Path, help='Archived run containing manifest.json')
    parser.add_argument('--output', type=Path, help='New restored archive directory')
    parser.add_argument('--profile', type=Path, help='Model/device settings for continuation')
    parser.add_argument('--continue', dest='continue_run', action='store_true')
    parser.add_argument('--prepare-only', action='store_true', help='Prepare continuation without calling model/device')
    parser.add_argument('--execution-output', type=Path, help='New Goal output directory')
    parser.add_argument('--goal', help='Goal for the continued execution; defaults to the archived goal')
    parser.add_argument('--install-only', action='store_true')
    parser.add_argument('--cache', type=Path, default=ROOT / '.restore')
    parser.add_argument('--python', type=Path, default=Path(sys.executable))
    parser.add_argument('--node', type=Path)
    parser.add_argument('--npu-python', type=Path, help='Existing Python with torch and torch_npu')
    args = parser.parse_args()
    if sys.version_info < (3, 11):
        parser.error('Python 3.11+ is required')
    if not args.install_only and not (args.run and args.output):
        parser.error('--run and --output are required')
    if (args.continue_run or args.prepare_only) and not args.profile:
        parser.error('--profile is required to prepare or execute a new Goal')
    if args.output and args.output.exists():
        parser.error('--output already exists; choose a new directory')
    cache = args.cache.absolute()
    cache.mkdir(parents=True, exist_ok=True)
    cache = cache.resolve()
    node = choose_node(args.node, cache)
    python = args.python.absolute()  # Preserve virtualenv path rather than resolving its symlink.
    print('Aligning Python packages, Pi SDK and LoopX...', flush=True)
    bundle, runtime, record = installed_copy(cache, node, python)
    if args.install_only:
        print(json.dumps(record, indent=2))
        return 0
    output = args.output.absolute()
    output.parent.mkdir(parents=True, exist_ok=True)
    run([node, bundle / 'examples/cann/restore-session.mjs', '--run', args.run.resolve(), '--output', output])
    save(output / 'environment.json', record)
    if args.continue_run or args.prepare_only:
        profile = json.loads(args.profile.read_text())
        profile = json.loads(json.dumps(profile).replace('{bundle}', str(bundle)))
        npu_python = args.npu_python or profile.get('npu_python')
        if npu_python is None:
            original_profile = json.loads(args.profile.read_text())
            old_python = original_profile.get('python', '')
            npu_python = old_python if old_python and '{bundle}' not in old_python and Path(old_python).is_file() else python
        profile.update(python=str(runtime / 'venv/bin/python'), node=str(node),
                       expected_python_modules=str(runtime / 'expected.json'),
                       loopx=str(runtime / 'venv/bin/loopx'), npu_python=str(Path(npu_python).absolute()))
        save(output / 'local-profile.json', profile)
        execution = args.execution_output.absolute() if args.execution_output else output.with_name(output.name + '-continued')
        argv = [node, bundle / 'examples/cann/continue-run.mjs', '--restored', output,
                '--profile', output / 'local-profile.json', '--output', execution]
        if args.prepare_only:
            argv.append('--prepare-only')
        if args.goal:
            argv.extend(['--goal', args.goal])
        run(argv)
    print(f'Restored: {output}')
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print(f'restore: {error}', file=sys.stderr)
        print('Installation details: .restore/installations/<identity>/bundle-*/bootstrap.log', file=sys.stderr)
        raise SystemExit(2)
