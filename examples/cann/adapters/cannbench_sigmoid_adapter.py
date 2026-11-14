"""Opt-in native RSI experiment over a frozen CANNBench Season 1 Sigmoid suite.

The host provides an immutable suite with cases.yaml, the original data generator,
a trusted Torch baseline, and the six-source reference. This is a local benchmark,
not a CANNBench submission. One sample is the mean of 30 NPU-event measurements
for one case; every one of the 20 cases must pass full-output precision checks.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import select
import signal
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'source/packages/executor/src'))
from native_rsi_adapter import canonical, digest, source, complexity


def save(path, value):
    path.write_text(json.dumps(value, indent=2, allow_nan=False))


def build(files, args):
    from kernelcat_executor.evaluation.evaluate import run_job
    from kernelcat_executor.evaluation.protocols.six_source.config import Request
    text = source(files)
    root = args.state / 'builds' / digest(files)
    if root.exists():
        receipt = json.loads((root / 'receipt.json').read_text())
        assert receipt['data']['candidate_sha256'] == hashlib.sha256(text.encode()).hexdigest()
        return root, receipt
    root.mkdir(parents=True)
    (root / 'candidate.py').write_text(text)
    if text == (args.suite / 'baseline.py').read_text():
        receipt = {'status': 'completed', 'data': {'trusted_baseline': True,
                   'candidate_sha256': hashlib.sha256(text.encode()).hexdigest()}}
    else:
        receipt = run_job(Request(root / 'candidate.py', args.suite / 'reference.py', root / 'job',
                                 'sigmoid', args.soc, args.device, True, 780))
    save(root / 'receipt.json', receipt)
    return root, receipt


def compare(actual, golden, dtype):
    import torch
    if actual.shape != golden.shape or actual.dtype != golden.dtype:
        return {'correct': False, 'reason': 'shape_or_dtype'}
    threshold = {'float16': 2**-10, 'bfloat16': 2**-7, 'float32': 2**-13}[dtype]
    total = 0.0; count = 0; maximum = 0.0
    a = actual.flatten(); g = golden.flatten()
    for offset in range(0, a.numel(), 1 << 20):
        x = a[offset:offset+(1<<20)].double(); y = g[offset:offset+(1<<20)].double()
        if not (torch.equal(torch.isnan(x), torch.isnan(y)) and
                torch.equal(torch.isposinf(x), torch.isposinf(y)) and
                torch.equal(torch.isneginf(x), torch.isneginf(y))):
            return {'correct': False, 'reason': 'nonfinite_positions'}
        finite = torch.isfinite(y)
        error = (x[finite]-y[finite]).abs() / (y[finite].abs()+1e-7)
        if error.numel():
            total += error.sum().item(); count += error.numel(); maximum = max(maximum,error.max().item())
    mean = total/max(count,1)
    return {'correct': mean < threshold and maximum < 10*threshold, 'mere': mean,
            'mare': maximum, 'threshold': threshold, 'compared_finite_elements': count}


def worker(root, suite, device):
    import torch
    import torch_npu
    import yaml
    torch.set_num_threads(4)
    torch_npu.npu.set_device(device)
    sys.path.insert(0, str(suite / 'generator'))
    from kernel_eval.data.data_generator import DataGenerator
    cases = yaml.safe_load((suite / 'cases.yaml').read_text())['cases']
    assert len(cases) == 20 and [c['case_id'] for c in cases] == list(range(1,21))
    receipt = json.loads((root/'receipt.json').read_text())
    runtime = None
    if receipt['data'].get('trusted_baseline'):
        fn = torch.sigmoid
    else:
        from kernelcat_executor.backends.ascend.runtime import AscendRuntime
        from kernelcat_executor.build.inputs import BuildSettings
        from kernelcat_executor.build.outputs import BuildOutputs, BuiltFile
        manifest = receipt['data']['result']['build']; build_root = root/'job/build'
        runtime = AscendRuntime(BuildSettings('sigmoid',build_root,manifest['target'],device))
        context = {}; exec(compile((root/'candidate.py').read_text(),'<candidate>','exec'),context)
        outputs = BuildOutputs(build_root,manifest['source_digest'],manifest['target'],
            tuple(BuiltFile(build_root/f['path'],f['sha256']) for f in manifest['files']))
        runtime.load(outputs,context['model_src'])
        fn = runtime.context['ModelNew']().to(f'npu:{device}')
    print(json.dumps({'ready':True,'name':torch_npu.npu.get_device_name(device),'device':device,
                      'torch':torch.__version__,'torch_npu':torch_npu.__version__}),flush=True)
    try:
        for line in sys.stdin:
            request = json.loads(line);case = cases[request['case_index']]
            generator = torch.Generator(device='cpu').manual_seed(request['seed'])
            cpu = DataGenerator().generate_input_tensor(case['input_shape'][0],case['dtype'][0],
                                                       case['value_range'],generator=generator)
            # CPU golden and candidate have independent storage; no candidate execution in the answer.
            golden = torch.sigmoid(cpu)
            x = cpu.to(f'npu:{device}'); torch_npu.npu.synchronize(device)
            with torch.no_grad(): y = fn(x)
            torch_npu.npu.synchronize(device)
            output = y.cpu(); observed = x.cpu()
            mutated = not torch.equal(cpu.view(torch.uint8), observed.view(torch.uint8))
            check = compare(output,golden,case['dtype'][0]);check['input_mutated']=mutated
            check['correct'] = check['correct'] and not mutated
            values = []
            if check['correct']:
                stream = torch_npu.npu.current_stream(device)
                with torch.no_grad(), torch_npu.npu.device(device):
                    for _ in range(3): y = fn(x)
                    torch_npu.npu.synchronize(device)
                    for _ in range(30):
                        begin=torch_npu.npu.Event(enable_timing=True);end=torch_npu.npu.Event(enable_timing=True)
                        begin.record(stream);y=fn(x);end.record(stream);torch_npu.npu.synchronize(device)
                        values.append(begin.elapsed_time(end))
            print(json.dumps({'case_id':case['case_id'],'check':check,'samples_ms':values}),flush=True)
            del cpu,golden,x,y,output,observed
            torch_npu.npu.empty_cache()
    finally:
        if runtime: runtime.close()


def receive(process, timeout=240):
    if not select.select([process.stdout],[],[],timeout)[0]: raise TimeoutError('original NPU worker timeout')
    text=process.stdout.readline()
    if not text: raise RuntimeError('original NPU worker exited')
    return json.loads(text)


def stop(process):
    if process.poll() is None:
        try: os.killpg(process.pid,signal.SIGKILL)
        except ProcessLookupError: pass
    process.wait()


def failure_feedback(receipt):
    """Preserve runtime/precision causes as well as primary compiler errors."""
    from kernelcat_executor.backends.ascend.compiler import diagnostic_lines
    data = receipt.get('data', {})
    native = data.get('result') or {}
    messages = [native.get('compile_info', ''), native.get('correctness_info', ''),
                data.get('diagnostic', ''), data.get('error', '')]
    artifact = data.get('artifact')
    if artifact:
        log = Path(artifact) / 'runner.log'
        if log.is_file():
            with log.open('rb') as stream:
                stream.seek(0, os.SEEK_END)
                stream.seek(max(0, stream.tell() - 32768))
                messages.insert(0, stream.read().decode(errors='replace'))
    return diagnostic_lines('\n'.join(str(message) for message in messages if message))


def measure(request,args,attempt):
    arms={};logs=[];samples=[];environments=[]
    try:
        for name in ('keep','remove'):
            root,receipt=build(request[name],args)
            arm={'root':str(root),'receipt':receipt,'correct':receipt['status']=='completed'};arms[name]=arm
            if not arm['correct']:continue
            env=os.environ.copy();custom=root/'job/build/opp/vendors/customize'
            env['LD_LIBRARY_PATH']=str(custom/'op_api/lib')+os.pathsep+env.get('LD_LIBRARY_PATH','')
            env['ASCEND_CUSTOM_OPP_PATH']=str(custom)
            log=(attempt/(name+'.worker.log')).open('wb');logs.append(log)
            process=subprocess.Popen([sys.executable,'-B',str(Path(__file__).resolve()),'--worker',str(root),str(args.suite),str(args.device)],
                stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=log,text=True,env=env,start_new_session=True)
            arm['process']=process
            try: environments.append(receive(process))
            except (RuntimeError,TimeoutError,ValueError) as error:
                arm['correct']=False;arm['error']=str(error);stop(process)
        for index in range(20):
            identity=request['sample_namespace']+'/case/'+str(index+1)
            row={'sample_id':identity,'seed':int(hashlib.sha256(identity.encode()).hexdigest()[:8],16)}
            for name in (('keep','remove') if index%2==0 else ('remove','keep')):
                arm=arms[name];process=arm.get('process');row[name]=None
                if process is None or process.poll() is not None:continue
                try:
                    process.stdin.write(json.dumps({'case_index':index,'seed':row['seed']})+'\n');process.stdin.flush()
                    response=receive(process);row[name]=response
                    times=response['samples_ms']
                    arm['correct'] &= response['check']['correct'] and len(times)==30 and all(math.isfinite(t) and t>0 for t in times)
                except (OSError,ValueError,RuntimeError,TimeoutError) as error:
                    arm['correct']=False;arm['error']=str(error);stop(process)
            samples.append(row);save(attempt/'samples.json',samples)
        evidence={'protocol':'cannbench-season1-sigmoid-20-full-output-v1','samples':samples,'environments':environments,
                  'builds':{n:a['receipt'] for n,a in arms.items()},'suite_sha256':args.suite_digest}
        save(attempt/'evidence.json',evidence)
        def features(name):
            arm=arms[name]
            diagnostics=[]
            for row in samples:
                observed=row[name]
                if observed and not observed['check']['correct']:
                    diagnostics.append('case '+str(observed['case_id'])+': '+json.dumps(observed['check']))
            if not arm['correct']:
                diagnostics.extend([arm['error']] if arm.get('error') else failure_feedback(arm['receipt']))
            else:
                means=[sum(row[name]['samples_ms'])/30 for row in samples]
                diagnostics.append('mean_us_by_case:'+json.dumps([round(x*1000,3) for x in means]))
            return {'surface':['AscendC' if not arm['receipt']['data'].get('trusted_baseline') else 'Torch baseline'],
                    'lowered':None,'runtime':['score:-log(milliseconds)','all-20-cases-required',*[x[:450] for x in diagnostics[:12]]]}
        def scores(name):
            return [-math.log(sum(row[name]['samples_ms'])/30) if row[name] and row[name]['samples_ms'] else 0 for row in samples]
        return {'measurement':{'measurement_id':'sigmoid:'+digest(request),'task_id':request['task_id'],
          'environment_id':'npu:'+digest(environments),'protocol_id':evidence['protocol'],
          'keep_digest':digest(request['keep']),'remove_digest':digest(request['remove']),
          'sample_ids':[r['sample_id'] for r in samples],'keep_log_scores':scores('keep'),'remove_log_scores':scores('remove'),
          'keep_correct':bool(arms['keep']['correct']),'remove_correct':bool(arms['remove']['correct']),
          'independent_confirmation':request['operation']=='confirm','evidence_ids':['sha256:'+digest(evidence),'artifact:'+str(attempt)],
          'features':features('keep'),'remove_features':features('remove'),
          'visible_coverage':['CANNBench-Season1-Sigmoid-cases-1-20','float16','bfloat16','float32','full-output-MERE-MARE','input-preservation'],
          'holdout_coverage':['new-seed-same-20-public-cases'] if request['operation']=='confirm' else None,
          'keep_complexity':complexity(request['keep']),'remove_complexity':complexity(request['remove'])}}
    finally:
        for arm in arms.values():
            if 'process' in arm:stop(arm['process'])
        for log in logs:log.close()


def main():
    if len(sys.argv)==5 and sys.argv[1]=='--worker':
        worker(Path(sys.argv[2]),Path(sys.argv[3]),int(sys.argv[4]));return
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state',type=Path,required=True);parser.add_argument('--suite',type=Path,required=True)
    parser.add_argument('--device',type=int,required=True);parser.add_argument('--soc',required=True)
    args=parser.parse_args();args.suite=args.suite.resolve();args.state=args.state.resolve()
    args.suite_digest=digest({str(p.relative_to(args.suite)):hashlib.sha256(p.read_bytes()).hexdigest()
                            for p in args.suite.rglob('*') if p.is_file() and '__pycache__' not in p.parts})
    action=json.load(sys.stdin);request=action['arguments']
    attempt=args.state/'attempts'/digest({'id':action['id'],'request':request})
    attempt.mkdir(parents=True,exist_ok=False);save(attempt/'action.json',action)
    if request['operation']=='decompose':
        before,after=source(request['baseline']),source(request['keep'])
        units=[] if before==after else [{'unit_id':'program:'+digest(request['keep'])[:16],'anchor':'SourceFile:solution.py',
          'path':'solution.py','before':before,'after':after,'dependencies':['solution.py'],'ku_ids':[]}]
        result={'decomposition':{'units':units,'candidate_complexity':complexity(request['keep']),
                                'baseline_complexity':complexity(request['baseline'])}}
    else:result=measure(request,args,attempt)
    response={'status':'completed','data':{'rsi':result}};save(attempt/'response.json',response)
    print(json.dumps(response,allow_nan=False))


if __name__=='__main__':main()
