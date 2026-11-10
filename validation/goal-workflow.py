"""Real SDK and LoopX protocol regression; synthetic device results, no NPU claim."""
from pathlib import Path
import json,hashlib,threading,http.server,subprocess,os,sys
import argparse,shutil
parser=argparse.ArgumentParser(description="Exercise real Pi/LoopX lifecycle with synthetic model and device receipts.")
parser.add_argument('--bundle',type=Path,default=Path(__file__).resolve().parents[1])
parser.add_argument('--output',type=Path,required=True)
parser.add_argument('--node',default=shutil.which('node'))
parser.add_argument('--multi-trial',action='store_true')
args=parser.parse_args()
bundle=args.bundle.resolve();runtime=bundle/'runtime';node=Path(args.node).resolve()
base=args.output.resolve();base.mkdir();label='multi' if args.multi_trial else 'complete'
out=base/label;fixture=base/(label+'-fixture');fixture.mkdir()
baseline=(bundle/'examples/cann/vector-add/baseline.py').read_text();reference=(bundle/'examples/cann/vector-add/reference.py').read_text();case=json.loads((bundle/'examples/cann/vector-add/case.json').read_text())
script=fixture/'tool.py';script.write_text('import json,sys,hashlib\nfrom pathlib import Path\na=json.load(sys.stdin)\nsource='+repr(baseline)+' if a["tool"]=="baseline_check" else Path("solution.py").read_text()\nprint(json.dumps({"status":"completed","data":{"candidate_sha256":hashlib.sha256(source.encode()).hexdigest(),"reference_sha256":'+repr(hashlib.sha256(reference.encode()).hexdigest())+',"result":{"compiled":True,"correctness":True,"failure_stage":None,"hardware":'+repr(case['soc'])+',"correctness_policy":'+repr({k:case[k] for k in ('atol','rtol')})+',"build":{"files":[{"path":"fixture","sha256":"fixture"}]},"performance":{"timer":"npu_event","num_trials":100,"samples_ms":[1.0]*100}},"fixture":True}}))\n')
class Handler(http.server.BaseHTTPRequestHandler):
 count=0
 def log_message(self,*args):pass
 def do_POST(self):
  body=json.loads(self.rfile.read(int(self.headers['Content-Length'])));i=Handler.count % 4;Handler.count+=1
  calls=[('baseline_check',{}),('write',{'path':'solution.py','content':baseline+'\n# protocol fixture candidate '+str(Handler.count)+'\n'}),('operator_evaluate',{})]
  if i<len(calls):
   name,args=calls[i];message={'role':'assistant','content':None,'tool_calls':[{'id':'fixture-'+str(i),'type':'function','function':{'name':name,'arguments':json.dumps(args)}}]};stop='tool_calls'
  else:message={'role':'assistant','content':'Completed fixture checks.'};stop='stop'
  raw=json.dumps({'id':'fixture-'+str(i),'object':'chat.completion','created':1,'model':'fixture','choices':[{'index':0,'message':message,'finish_reason':stop}],'usage':{'prompt_tokens':100,'completion_tokens':20,'total_tokens':120}}).encode()
  self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler);threading.Thread(target=server.serve_forever,daemon=True).start()
profile={'schema_version':'kernelbridge.cann-profile/v1','python':str(runtime/'venv/bin/python'),'node':str(node),'expected_python_modules':str(runtime/'expected.json'),'loopx':str(runtime/'venv/bin/loopx'),'diagnostics':False,'goal':{'max_trials':2,'complete_on_acceptance':not args.multi_trial},'config':{'credits':1000,'policy':{'max_steps':8,'context_bytes':524288},'pi_worker':{'timeout':240},'providers':{'primary':{'provider':'openai','base_url':f'http://127.0.0.1:{server.server_port}/v1','model':'fixture','api_key_env':'KB_FIXTURE_KEY','allow_insecure_http':True,'max_tokens':8192,'wire_options':{'native_turns':True,'include_parallel_tool_calls':False}}},'business_tools':[{'name':name,'description':'Protocol fixture, synthetic device outcome','argv':[str(Path(sys.executable).resolve()),str(script)],'files':[{'path':str(script),'sha256':hashlib.sha256(script.read_bytes()).hexdigest()}],'parameters':{'type':'object','properties':{},'additionalProperties':False}} for name in ('baseline_check','operator_check','operator_evaluate')]}}
p=fixture/'profile.json';p.write_text(json.dumps(profile));env=os.environ.copy();env['KB_FIXTURE_KEY']='protocol-fixture';env['PYTHONDONTWRITEBYTECODE']='1'
with (base/(label+'.log')).open('w') as log:
 r=subprocess.run([str(node),str(bundle/'examples/cann/run.mjs'),'--task',str(bundle/'examples/cann/vector-add/task.json'),'--profile',str(p),'--output',str(out)],env=env,stdout=log,stderr=subprocess.STDOUT)
server.shutdown();print(json.dumps({'exit_code':r.returncode,'calls':Handler.count,'log':str(base/(label+'.log'))}));print((base/(label+'.log')).read_text()[-3000:])
assert r.returncode == 0
result=json.loads((out/'sdk/result.json').read_text())
turns=json.loads((out/'sdk/goal-turns.json').read_text())
assert result['status'] == ('progress' if args.multi_trial else 'completed'), result
assert len(turns) == (2 if args.multi_trial else 1)
assert json.loads((out/'acceptance.json').read_text())['accepted'] is True
if args.multi_trial:
 assert len({t['turnKey'] for t in turns}) == 2
 assert result['pursuit_status'] == 'invocation_limit'
else:
 assert json.loads((out/'sdk/goal-after.json').read_text())['acceptance']['goal_acceptance_contract']['status'] == 'accepted'
(base/'assertions.json').write_text(json.dumps({'passed':True,'synthetic_device':True,'model_calls':Handler.count,'trials':len(turns),'status':result['status']},indent=2)+'\n')
