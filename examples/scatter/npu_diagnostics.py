"""Derive LLVM IR from the original CANN compiler recipe without rerunning a kernel."""
import hashlib
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import time


def collect_ir(job):
    job = Path(job).resolve()
    folder = job / 'ir'; folder.mkdir(exist_ok=False)
    sdk = Path(os.environ['ASCEND_HOME_PATH']).resolve()
    records = []
    for recipe in sorted((job / 'build').rglob('*.mk')):
        for line in recipe.read_text().splitlines():
            if not line.startswith('\t'): continue
            original = shlex.split(line.strip())
            if not original or Path(original[0]).name not in {'bisheng', 'ccec'} or '-E' in original or '-c' not in original:
                continue
            compiler = Path(original[0]).resolve(strict=True)
            if not compiler.is_relative_to(sdk): raise ValueError('Compiler is outside the pinned SDK')
            inputs = [Path(arg).resolve() for arg in original if arg.endswith('.cpp') and not arg.startswith('-')]
            if not inputs or any(not path.is_relative_to(job) for path in inputs):
                raise ValueError('IR recipe does not compile the original frozen job')
            output = folder / f'kernel-{len(records):03d}.ll'
            argv = [arg for arg in original if arg not in {'-c', '-save-temps', '--save-temp-files'}]
            argv[argv.index('-o') + 1] = str(output)
            argv += ['-S', '-emit-llvm']
            record = {'recipe': str(recipe.relative_to(job)), 'recipe_sha256': hashlib.sha256(recipe.read_bytes()).hexdigest(),
                      'original_argv': original, 'argv': argv, 'compiler_sha256': hashlib.sha256(compiler.read_bytes()).hexdigest(),
                      'started_ns': time.time_ns()}
            log = folder / f'kernel-{len(records):03d}.log'
            records.append(record)
            try:
                with log.open('wb') as stream:
                    process = subprocess.Popen(argv, cwd=folder, stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
                    record['pid'] = process.pid
                    (folder / 'receipt.json').write_text(json.dumps(records, indent=2) + '\n')
                    record['returncode'] = process.wait(timeout=90)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL); process.wait(); record['error'] = 'IR compiler timeout; process group killed and reaped'
            record['ended_ns'] = time.time_ns()
            (folder / 'receipt.json').write_text(json.dumps(records, indent=2) + '\n')
    return records
