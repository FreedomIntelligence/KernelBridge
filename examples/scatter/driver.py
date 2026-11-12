"""Real local compiler/correctness/timing driver for the no-Torch pipeline.

One JSON request on stdin. Outputs local CPU evidence only. No NPU or official
qualification is inferred. C++ code and oracle are selected by frozen paths.
"""
import ctypes
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import random
import shutil
import struct
import subprocess
import sys
import time


def sha(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def cxx_compiler():
    """CXX, else Apple's SDK-matched clang++ on macOS, else clang++/g++ on PATH.

    A conda clang++ ahead of /usr/bin on PATH is older than the SDK's libc++
    ("Libc++ only supports Clang 16 and later") and fails on <cmath>.
    """
    if os.environ.get('CXX'):
        return shutil.which(os.environ['CXX']) or os.environ['CXX']
    # /usr/bin/clang++ is Apple's xcrun shim: it selects the SDK, so <cmath> resolves even
    # in a minimal environment (the toolchain binary called directly does not).
    if sys.platform == 'darwin' and os.access('/usr/bin/clang++', os.X_OK):
        return '/usr/bin/clang++'
    return shutil.which('clang++') or shutil.which('g++')


def run(request, mode):
    source = Path(request['source']['root'])
    output = Path(request['output'])
    compiler = cxx_compiler()
    if not compiler: raise ValueError('C++ compiler required')
    library = output / 'scatter.so'
    argv = [compiler, '-std=c++17', '-O2', '-shared', '-fPIC', str(source / 'native.cpp'), '-o', str(library)]
    result = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    (output / 'build.log').write_text(result.stdout + result.stderr)
    if result.returncode: raise ValueError('Native compilation failed')
    native = ctypes.CDLL(str(library)); function = native.scatter_extrema
    function.argtypes = [ctypes.POINTER(ctypes.c_float), ctypes.POINTER(ctypes.c_int64), ctypes.c_int64,
                         ctypes.c_int64, ctypes.c_int64, ctypes.c_int, ctypes.POINTER(ctypes.c_float)]
    function.restype = ctypes.c_int
    oracle_record = request['contract']['oracle']['files']
    oracle_path = Path(oracle_record['root']) / 'reference.py'
    spec = importlib.util.spec_from_file_location('scatter_oracle', oracle_path)
    oracle = importlib.util.module_from_spec(spec); spec.loader.exec_module(oracle)
    cases = request['contract']['suite']
    timings = {}; passed = []
    for case in cases:
        rng = random.Random(case['seed']); n, f, b = case['rows'], case['features'], case['bins']
        data = [[struct.unpack('f', struct.pack('f', rng.uniform(-4, 4)))[0] for _ in range(f)] for _ in range(n)]
        indices = [rng.randrange(b) for _ in range(n)]
        if n > 1 and case.get('duplicates'): data[1] = data[0][:]; indices[1] = indices[0]
        if case.get('all_one'): indices = [0] * n
        x = (ctypes.c_float * (n * f))(*(v for row in data for v in row)); index = (ctypes.c_int64 * n)(*indices)
        out = (ctypes.c_float * (b * f))()
        maximum = int(case['reduce'] == 'max')
        def invoke():
            if function(x, index, n, f, b, maximum, out): raise ValueError('Native input rejection')
        invoke()
        expected = oracle.scatter(data, indices, b, case['reduce']) if n else [[0.0] * f for _ in range(b)]
        actual = [list(out[row * f:(row + 1) * f]) for row in range(b)]
        if actual != expected: raise ValueError('Correctness failure: ' + case['id'])
        passed.append(case['id'])
        if mode == 'measure':
            measurement = request['contract']['measurement']
            for _ in range(measurement.get('warmup', 3)): invoke()
            samples = []
            for _ in range(measurement.get('samples', 9)):
                start = time.perf_counter_ns(); invoke(); samples.append((time.perf_counter_ns() - start) / 1000)
            timings[case['id']] = samples
    return {'checks': {'build': True, 'semantics': True, 'full': True}, 'correct': True, 'case_count': len(passed),
            'case_ids': passed, 'oracle_sha256': oracle_record['sha256'], 'scope': 'local_cpu',
            'environment': {'machine': platform.machine(), 'platform': platform.platform(), 'compiler_sha256': sha(compiler)},
            'measurement': request['contract']['measurement'], 'cases': timings,
            'binary_sha256': sha(library), 'build_argv': argv, 'device_acceptance': False}


if __name__ == '__main__':
    print(json.dumps(run(json.load(sys.stdin), sys.argv[1])))
