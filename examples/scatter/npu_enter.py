"""Enter one explicitly supplied CANN/Python profile before importing device code."""
import json
import hashlib
import os
from pathlib import Path
import subprocess
import sys


def verify_isolated_visibility(env):
    """New guarded jobs cannot silently remap runtime device zero after SDK setup."""
    original = os.environ.get('KERNELBRIDGE_ISOLATION_REQUEST_PATH')
    expected = os.environ.get('KERNELBRIDGE_ISOLATION_REQUEST_SHA256')
    if original is None and expected is None:
        return
    if not original or not expected:
        raise ValueError('Incomplete original device-isolation entry binding')
    path = Path(original)
    if not path.is_absolute() or path.resolve(strict=True) != path or not path.is_file():
        raise ValueError('Original device-isolation entry request is linked or invalid')
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError('Original device-isolation entry request changed')
    request = json.loads(raw)
    if (request.get('isolation_policy', {}).get('required') is not True
            or request.get('npu_enter_code_sha256') != hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
            or request['isolation_policy']['logical_device'] != request['intent']['device']):
        raise ValueError('Original device-isolation entry policy or code changed')
    for name in ('ASCEND_VISIBLE_DEVICES', 'ASCEND_RT_VISIBLE_DEVICES'):
        if env.get(name, ''):
            raise ValueError('Device-isolated execution does not support a visibility remap: ' + name)


def main():
    profile = json.loads(Path(sys.argv[1]).read_text())
    sdk = Path(profile['sdk']).resolve(strict=True)
    python = Path(profile['python']).absolute()
    env = dict(os.environ)
    for key in list(env):
        if key.startswith(('ASCEND_', 'TOOLCHAIN_HOME')) and key not in ('ASCEND_VISIBLE_DEVICES', 'ASCEND_RT_VISIBLE_DEVICES'):
            env.pop(key)
    for key in ('PATH', 'PYTHONPATH', 'LD_LIBRARY_PATH', 'LIBRARY_PATH', 'CPATH', 'CMAKE_PREFIX_PATH'):
        env[key] = ':'.join(p for p in env.get(key, '').split(':') if p and '/sdk/' not in p
                              and ('/Ascend/' not in p or '/Ascend/driver/' in p))
    values = subprocess.check_output(['bash', '-c', 'source "$1" >/dev/null && env -0', 'sdk-env', str(sdk / 'set_env.sh')], env=env)
    env = dict(item.decode().split('=', 1) for item in values.split(b'\0') if item)
    if Path(env['ASCEND_HOME_PATH']).resolve() != sdk:
        raise ValueError('SDK activation does not match the pinned profile')
    verify_isolated_visibility(env)
    env['PATH'] = str(python.parent) + ':' + env['PATH']
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    env['PYTHON'] = str(python)
    if profile.get('executor_source'):
        env['PYTHONPATH'] = profile['executor_source'] + ':' + env.get('PYTHONPATH', '')
    if profile.get('cmake_toolchain'):
        env['CMAKE_TOOLCHAIN_FILE'] = profile['cmake_toolchain']
    if profile.get('python_include'):
        env['CPATH'] = profile['python_include'] + ':' + env.get('CPATH', '')
    os.execve(python, [str(python), '-B', *sys.argv[2:]], env)


if __name__ == '__main__': main()
