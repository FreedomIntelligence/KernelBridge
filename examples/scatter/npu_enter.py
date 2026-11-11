"""Enter one explicitly supplied CANN/Python profile before importing device code."""
import json
import os
from pathlib import Path
import subprocess
import sys


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
