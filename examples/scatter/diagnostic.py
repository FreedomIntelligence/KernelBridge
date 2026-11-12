"""Local compiler IR/ISA and CPU profiling drivers for pipeline stage J.

Device profiling remains the explicit CANN profiler_collect tool. These outputs
are CPU diagnostics, never NPU timing, PC attribution or official acceptance.
"""
import csv
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

from driver import cxx_compiler, run as measure


def run(request, kind):
    output = Path(request['output']); source = Path(request['source']['root']) / 'native.cpp'
    artifacts = []
    if kind == 'ir':
        compiler = cxx_compiler()
        if not compiler or 'clang' not in Path(compiler).name: raise ValueError('clang++ is required for local LLVM collection')
        for level in ('0', '2'):
            path = output / ('kernel-O' + level + '.ll')
            result = subprocess.run([compiler, '-std=c++17', '-S', '-emit-llvm', '-O' + level, str(source), '-o', str(path)], capture_output=True, text=True, timeout=60)
            (output / ('compile-O' + level + '.log')).write_text(result.stderr)
            if result.returncode: raise ValueError('LLVM collection failed')
            artifacts.append(path)
        assembly = output / 'kernel.s'
        subprocess.run([compiler, '-std=c++17', '-S', '-O2', str(source), '-o', str(assembly)], check=True, capture_output=True, timeout=60)
        artifacts.append(assembly)
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'cann/tools'))
        from ir_bridge import render_ir, llvm_stats
        rendering = render_ir(artifacts[:2], output / 'ir.html', 'llvm')
        parsed = [llvm_stats(p.read_text()) for p in artifacts[:2]]
        artifacts.extend([output / 'ir.html', output / 'ir.json'])
        details = {'rendering': rendering, 'statistics': parsed, 'compiler': str(compiler),
                   'compiler_sha256': hashlib.sha256(Path(compiler).read_bytes()).hexdigest()}
    elif kind == 'profiler':
        report = measure(request, 'measure')
        path = output / 'profile.csv'
        with path.open('w', newline='') as stream:
            writer = csv.writer(stream); writer.writerow(['case', 'sample', 'cpu_us'])
            for case, samples in report['cases'].items():
                for index, value in enumerate(samples): writer.writerow([case, index, value])
        artifacts.append(path); details = {'case_count': report['case_count'], 'environment': report['environment']}
    else: raise ValueError('Unknown diagnostic kind')
    return {'kind': kind, 'scope': 'local_cpu_diagnostic', 'candidate_sha256': request['source']['sha256'],
            'artifacts': {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in artifacts},
            'details': details, 'device_acceptance': False, 'performance_claim': None}


if __name__ == '__main__': print(json.dumps(run(json.load(sys.stdin), sys.argv[1])))
