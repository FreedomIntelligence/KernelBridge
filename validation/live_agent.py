"""Opt-in live model acceptance; credentials come only from the named environment variable.

Run with --output NEW_DIRECTORY --scenario generate|repair|knowledge|npu.
NPU mode requires --evaluator pointing to a provisioned JSON evaluator adapter.
"""
from pathlib import Path
import argparse, hashlib, json, os, shutil, sys
ROOT=Path(__file__).resolve().parents[1]
for source in [*ROOT.glob('source/packages/*/src'), ROOT/'source/apps/worker/src']:
    sys.path.insert(0,str(source))
from kernelcat_executor.capabilities.filesystem import Workspace
from kernelcat_executor.capabilities.skills import Skills
from kernelcat_executor.capabilities.workspace_policy import WorkspaceGuard
from kernelcat_executor.capabilities.workspace_tools import builtin_tools, FunctionTool, schema
from kernelcat_executor.capabilities.process import CommandEvaluator
from kernelcat_executor.capabilities.llm import APIProposer
from kernelcat_executor.contracts.providers import WireOptions
from kernelcat_executor.contracts.models import Task, Policy, Outcome, ToolSpec
from kernelcat_executor.operations.gateway import Gateway
from kernelcat_executor.operations.session import ExecutionSession
from kernelcat_executor.profiles.store import AgentJournal
from kernelcat_worker.replay import NativeReplayFiles
from kernelcat_knowledge.storage.filesystem import RawReader


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--scenario',choices=['generate','repair','knowledge','npu','npu-generate'],default='generate')
    p.add_argument('--base-url',default='https://api.deepseek.com/v1')
    p.add_argument('--model',default='deepseek-flash')
    p.add_argument('--api-key-env',default='DEEPSEEK_API_KEY')
    p.add_argument('--evaluator',type=Path)
    args=p.parse_args();out=args.output.resolve();out.mkdir(parents=True,exist_ok=False)
    workspace=Workspace(out/'workspace');(workspace.root/'reference').mkdir()
    repair=args.scenario in {'repair','npu'}
    native=args.scenario.startswith('npu')
    if native:
        source=(ROOT/'validation/fixtures/ascend_add.py').read_text().replace('a.GetValue(i)+b.GetValue(i)','a.GetValue(i)-b.GetValue(i)')
        spec='Implement native FP32 vector addition using the six-source Ascend C format in solution.py. Preserve all six fields and the custom operator name. Inputs are contiguous FP32 vectors, current acceptance uses length 128. The current implementation may be wrong. No CPU/Torch computation fallback is allowed.'
        if args.evaluator is None:p.error('NPU mode requires --evaluator')
    else:
        source='def add(x, y):\n    return [a-b for a,b in zip(x,y)]\n'
        spec='Implement solution.py with def add(x,y) returning elementwise sums of equal-length Python numeric lists, preserving both inputs. Empty lists must work. Unequal lengths must raise ValueError. No imports, I/O, attributes, or external libraries; ordinary Python builtins and arithmetic suffice.'
    if repair:(workspace.root/'solution.py').write_text(source)
    if args.scenario=='npu-generate':
        template=source.replace('for(uint32_t i=0;i<t.size;++i) c.SetValue(i,a.GetValue(i)-b.GetValue(i));','// TODO: implement vector addition over all t.size elements using a, b, c.')
        (workspace.root/'reference/native-template.py').write_text(template)
        spec+=' Start from reference/native-template.py, fill its missing kernel and write the complete candidate to solution.py.'
    (workspace.root/'reference/spec.md').write_text(spec)
    knowledge_root=out/'knowledge/raw';knowledge_root.mkdir(parents=True)
    (knowledge_root/'vector-add.txt').write_text('Vector addition: validate lengths, preserve inputs, return a fresh list of pairwise sums. Empty inputs yield an empty list.')
    class Loader:
        def find_by_id(self,_):return None
    reader=RawReader(raw_root=knowledge_root,repo_root=knowledge_root.parent,loader=Loader())
    knowledge=FunctionTool(ToolSpec(name='knowledge_search',description='Search the local raw knowledge corpus; returns source paths.',parameters=schema({'query':{'type':'string'}},['query']),credits=1),lambda a:Outcome(data=reader.grep(a.arguments['query'])))
    goal=spec+' Read reference/spec.md, implement and use evaluate(candidate="solution.py") to verify. Read the original evaluation receipt with read_receipt, then finish with evidence. Keep reference read-only.'
    if repair:goal+=' First evaluate the existing implementation before editing, then fix actual evaluator errors and evaluate again. Never finish with a failed evaluation.'
    if args.scenario=='knowledge':goal+=' Before coding, search knowledge for "Vector addition", and for "missing-acceptance-marker" to check the empty-result path.'
    store=AgentJournal(out/'state/events.jsonl');store.initialize(400,Policy(max_steps=24,context_bytes=262144))
    task=Task(id='live-'+args.scenario,scope='acceptance',goal=goal)
    provider=APIProposer('openai',args.base_url,args.model,os.environ[args.api_key_env],max_tokens=8192,timeout=120,wire_options=WireOptions(native_turns=True,include_parallel_tool_calls=False,tool_choice='auto',thinking='disabled'))
    evaluator=CommandEvaluator((sys.executable,'-B',str((args.evaluator or ROOT/'validation/live_cpu_evaluator.py').resolve())),workspace,
        schema({'candidate':{'type':'string'}},['candidate']),timeout=720 if native else 30,
        environment={'PATH':os.environ['PATH'],'LANG':'en_US.UTF-8'},artifact_root=out/'evaluations')
    gateway=Gateway(store,[*builtin_tools(workspace,Skills(())),evaluator,knowledge],task.scope,(WorkspaceGuard(workspace.root,('solution.py',)),))
    replay=NativeReplayFiles(store,out/'state',workspace.root,task.id)
    session=ExecutionSession(gateway,{'primary':provider},[],cwd=workspace.root,argv=(shutil.which('node'),str(ROOT/'source/apps/agent/src/harness/pi/worker.mjs')),
        external_context=True,timeout=780,replay=replay,workspace_policy={'root':str(workspace.root),'writable_paths':['solution.py'],'readonly_paths':['reference'],'codemode':False})
    result=session.run(task)
    rows=list(store.snapshot()['actions'].values());evaluations=[r for r in rows if r['action']['tool']=='evaluate']
    tools=[r['action']['tool'] for r in rows]
    checks={'finished':result['status']=='finished','evaluated':bool(evaluations),
        'final_evaluation_passed':bool(evaluations) and evaluations[-1]['outcome']['status']=='completed',
        'source_bound':bool(evaluations) and evaluations[-1]['outcome']['data'].get('candidate_sha256')==hashlib.sha256((workspace.root/'solution.py').read_bytes()).hexdigest(),
        'receipt_read':'read_receipt' in tools,'wrote_candidate':bool({'write','edit'}&set(tools)),
        'reference_preserved':(workspace.root/'reference/spec.md').read_text()==spec}
    if repair:checks['observed_failure_then_repair']=len(evaluations)>=2 and evaluations[0]['outcome']['status']=='failed' and evaluations[-1]['outcome']['status']=='completed'
    if args.scenario=='knowledge':
        hits=[r['outcome']['data'].get('returned') for r in rows if r['action']['tool']=='knowledge_search']
        checks['knowledge_hit_and_miss']=0 in hits and any(n is not None and n>0 for n in hits)
    before=len(store.snapshot()['actions']);resumed=session.run(task)
    checks['resume_without_new_effects']=resumed==result and len(store.snapshot()['actions'])==before
    report={'model':args.model,'base_url':args.base_url,'scenario':args.scenario,'checks':checks,'result':result,'tools':tools,'evaluations':evaluations,'online_inference':True}
    (out/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'checks':checks,'report':str(out/'result.json')},ensure_ascii=False),flush=True)
    return 0 if all(checks.values()) else 1
if __name__=='__main__':raise SystemExit(main())
