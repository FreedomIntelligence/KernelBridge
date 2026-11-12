"""Prepare one 20-round model campaign; no model or device is invoked here."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import sys

from ascend_sources import sources

HERE = Path(__file__).resolve().parent


def save(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n')


def prepare(args):
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    project = root / 'project'; project.mkdir()
    state = root / 'trials'; state.mkdir()
    candidate = project / 'candidate.py'
    if args.seed:
        shutil.copyfile(args.seed, candidate)
    else:
        # Literal strings keep the six-source loader strict, with editable C++ lines.
        candidate.write_text('\n\n'.join(name + ' = """' + value.replace('\\', '\\\\').replace('"""', '\\"\\"\\"') + '"""'
                                          for name, value in sources(args.mode, 33).items()) + '\n')
    shutil.copyfile(candidate, root / 'initial.py')
    contract = {
        'operator': 'feature-index scatter ' + args.mode,
        'inputs': 'finite FP32 contiguous src[N,F], int64 index[N], 0 <= index < 33',
        'outputs': 'FP32 [33,F], dim=0 reduction, values only, empty bins zero',
        'oracle': 'independent CPU per-bin amax/amin, excluded from timing',
        'baseline': 'torch_npu eager broadcast mask + amax/amin; torch scatter_reduce CPU fallback is excluded',
        'cases': [{'rows': n, 'features': f, 'family': family} for n, f, family in
                  [(1, 1, 'sparse'), (17, 7, 'sparse'), (129, 65, 'uniform'),
                   (256, 128, 'collision'), (33, 257, 'sorted'), (513, 32, 'hot')]],
        'measurement': {'scope': 'same_device_npu_eager_abba', 'device': args.device,
                        'warmup': 3, 'repeats': 10, 'pairs': 3,
                        'allocation': 'both APIs allocate output', 'unit': 'us'},
        'qualification': 'Each candidate is paired with eager. Across-round ranking is best observed, not paired promotion.',
    }
    workflow_contract = {
        'task': {'operator': contract['operator'], 'inputs': contract['inputs'], 'outputs': contract['outputs']},
        'oracle': {'kind': 'independent', 'description': contract['oracle'],
                   'files': {'npu_benchmark.py': str(HERE / 'npu_benchmark.py'), 'npu_eager.py': str(HERE / 'npu_eager.py')}},
        'suite': [dict(case, id=f"{case['family']}-{case['rows']}x{case['features']}") for case in contract['cases']],
        'measurement': contract['measurement'],
        'qualification': contract['qualification'],
    }
    save(project / 'contract.json', workflow_contract)
    config = {'mode': args.mode, 'device': args.device, 'candidate': str(candidate),
              'initial_candidate': str(root / 'initial.py'),
              'state': str(state), 'transport_module': str(args.transport.resolve(strict=True)),
              'remote_root': args.remote_root, 'warmup': 3, 'repeats': 10, 'pairs': 3,
              'wait_seconds': 900, 'poll_seconds': 5, 'profile_rounds': [1, 20],
              'pipeline_state': str(root / 'operator-pipeline')}
    config_path = root / 'trial-config.json'; save(config_path, config)
    pins = sorted(HERE.glob('npu_*.py'))
    pins.extend([HERE.parent / 'operator_pipeline/diagnostic_policy.py', config_path,
                 root / 'initial.py', project / 'contract.json', args.transport.resolve()])
    names = {'launch': 'npu-launch', 'collect': 'npu-collect', 'check': 'npu-check',
             'measure': 'npu-measure', 'ir': 'npu-ir', 'profiler': 'npu-profiler'}
    profile = {'read_roots': [str(root), str(HERE)], 'drivers': {
        name: {'kind': 'diagnostic' if operation in ('ir', 'profiler') else operation,
               'argv': [str(args.python.absolute()), '-B', str(HERE / 'npu_pipeline_driver.py'), str(config_path), operation],
               'files': [str(path) for path in pins], 'timeout': 1800 if operation in ('launch', 'collect') else 180}
        for operation, name in names.items()},
        'diagnostics': {'on_failure': False, 'on_regression': False, 'required': False,
                        'drivers': {'ir': names['ir'], 'profiler': names['profiler']}, 'scope': 'npu_diagnostic'}}
    save(root / 'workflow-profile.json', profile)
    spec = importlib.util.spec_from_file_location('npu_workflow_prepare', HERE.parent / 'operator_pipeline/prepare.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    tools = module.prepare(profile, root / 'pipeline-tools', args.python)
    for tool in tools:
        tool['capture_paths'] = ['candidate.py']
        if tool['name'] == 'pipeline_pair':
            tool['description'] = 'G: project the original E job ABBA samples into four original arm receipts. No new measurement occurs. Inspect full-case speedups. J drivers make original source-bound IR/profiler evidence available for investigating regressions and revising hypotheses in C.'
        elif tool['name'] == 'pipeline_run':
            tool['description'] = 'E: npu-launch executes exactly one frozen NPU job, npu-collect observes it. F: npu-check validates its original complete-suite receipt. J: npu-ir/npu-profiler recover original source-bound diagnostic artifacts without a new job. Unknown requires collecting the same original handle, never replacement launch.'
    save(root / 'business-tools.json', tools)
    inputs = {'contract': workflow_contract,
              'baseline': {'selection': 'files', 'files': {'npu_eager.py': str(HERE / 'npu_eager.py')}},
              'freeze': {'files': {'candidate.py': str(candidate)}}, 'drivers': names,
              'execution': 'E runs one asynchronous native job containing build, correctness and three original ABBA groups; F/G/J are read-only projections of that result, not repeated benchmarks.',
              'diagnostics': 'J npu-ir and npu-profiler expose original diagnostic artifacts; pipeline_read reads their actual bytes. Native automatic collection, model J calls, artifact reading and C analysis are distinct events.',
              'calibration': 'unavailable: no official task/website calibration driver; I may archive local accepted evidence only'}
    save(project / 'workflow-inputs.json', inputs)
    (project / 'AGENTS.md').write_text(
        'This is an authorized Ascend NPU optimization experiment. Edit only candidate.py.\n'
        'Keep exactly six literal string assignments: project_json_src, host_tiling_src, host_operator_src, '
        'kernel_src, python_bind_src, model_src. Keep the custom_ops_lib.run(src,index) ABI.\n'
        'Read workflow-inputs.json, contract.json, candidate.py and feedback.json. Preserve the frozen semantics and all cases. '
        'Change an actual execution mechanism: parallelism, tiling, vectorization, data movement, scheduling or allocation. '
        'No comment-only changes, renaming-only changes, input-value/seed specialization, cached answers, CPU fallback, '
        'benchmark/oracle modifications, network or direct device commands. Shape specialization must remain mathematically valid.\n'
        'Read pipeline_status workflow.next_actions and workflow-inputs.json to operate the A-J tools. '
        'E npu-launch owns the complete native execution; F/G/J read its original receipts. '
        'J npu-ir and npu-profiler make original diagnostics available, and pipeline_read reads their actual bytes. '
        'Native automatic collection does not mean the model has read or analyzed that evidence. '
        'Do not start a second E or a new candidate within the same coordinator round. '
        'On a positive complete pair, H may promote and I archive local evidence; official calibration is unavailable. '
        'IR unsupported is unavailable, never a completed analysis. A failed build/check is a failed experiment. Unknown means recover the original job. '
        'Finish the requested round with measured results and stop. The coordinator requests the next round. '
        'No delegation and no independent benchmark. Results are local device evidence, not official task qualification.\n')
    return {'campaign': str(root), 'mode': args.mode, 'device': args.device}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--mode', required=True, choices=['max', 'min'])
    parser.add_argument('--device', required=True, type=int)
    parser.add_argument('--transport', required=True, type=Path)
    parser.add_argument('--remote-root', required=True)
    parser.add_argument('--python', type=Path, default=Path(sys.executable))
    parser.add_argument('--seed', type=Path)
    print(json.dumps(prepare(parser.parse_args())))
