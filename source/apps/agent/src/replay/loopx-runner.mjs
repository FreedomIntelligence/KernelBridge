import { spawn } from 'node:child_process';
import { readFile, rename, mkdir, open, unlink } from 'node:fs/promises';
import { dirname } from 'node:path';
import { pathToFileURL } from 'node:url';
import { claimGoalOwner } from '../sdk/loopx/ownership.mjs';

/** One bounded CLI effect. LoopX, not this launcher, validates and settles Turns. */
export function callCli(argv,{cwd,timeoutMs,signal,maxBytes=8*1024*1024}) {
  return new Promise((resolve,reject)=>{
    let child,done=false,total=0,out='',failure=null,escalation;
    const finish=(error,value)=>{
      if(done)return;done=true;clearTimeout(timer);clearTimeout(escalation);
      signal?.removeEventListener('abort',abort);error?reject(error):resolve(value);
    };
    const stop=(error)=>{
      if(failure||done)return;failure=error;
      // LoopX's Python transport closes its supervisor control pipe on Ctrl-C.
      // The official supervisor then drains its owned host process group.
      child?.kill('SIGINT');
      escalation=setTimeout(()=>child?.kill('SIGKILL'),8000);
    };
    const abort=()=>stop(new Error('Control-plane invocation interrupted; inspect the recorded Turn key.'));
    const timer=setTimeout(abort,timeoutMs);
    try {
      // Remain in the caller-owned process group: the bounded Python transport
      // can reap this CLI too if the Node launcher itself fails.
      child=spawn(argv[0],argv.slice(1),{cwd,env:process.env,stdio:['ignore','pipe','pipe']});
    } catch(error){finish(error);return;}
    signal?.addEventListener('abort',abort,{once:true});
    child.once('error',error=>finish(error));
    for(const [label,stream] of [['out',child.stdout],['err',child.stderr]]){
      stream.setEncoding('utf8');stream.on('data',chunk=>{
        total+=Buffer.byteLength(chunk);
        if(total>maxBytes){stop(new Error('Control-plane output limit; inspect its journal.'));return;}
        if(label==='out')out+=chunk;
      });
    }
    child.once('close',code=>{
      if(failure){finish(failure);return;}
      try {
        const value=JSON.parse(out);
        if(!value||typeof value!=='object'||Array.isArray(value))throw new Error('Invalid CLI response');
        // A typed failure is an observation, never permission to retry.
        finish(null,{...value,_exitCode:code});
      } catch {finish(new Error('Control-plane response unavailable; inspect the recorded Turn key.'));}
    });
    if(signal?.aborted)abort();
  });
}

const keyPattern=/^sha256:[a-f0-9]{64}$/;
async function saveCursor(path,value){
  await mkdir(dirname(path),{recursive:true,mode:0o700});
  const temporary=`${path}.${process.pid}.tmp`;
  const file=await open(temporary,'wx',0o600);
  try {await file.writeFile(JSON.stringify(value)+'\n');await file.sync();}
  finally {await file.close();}
  try {await rename(temporary,path);}
  finally {await unlink(temporary).catch(e=>{if(e.code!=='ENOENT')throw e;});}
  const directory=await open(dirname(path),'r');
  try {await directory.sync();} finally {await directory.close();}
}

function decisionArgs(p,index){
  return ['--goal-id',p.goalId,'--agent-id',p.agentId,'--host','generic-cli',
    '--execution-mode','isolated-headless','--scheduler-owner','outer_controller',
    '--todo-id',p.todoId,'--turn-instance-id',`${p.cycleId}-${index}`,
    '--iteration-context','fresh','--scan-root',p.project];
}
function executionArgs(p,index,key){
  const args=decisionArgs(p,index);
  if(key){
    for(const flag of ['--turn-instance-id','--todo-id']){const at=args.indexOf(flag);args.splice(at,2);}
    args.push('--resume-turn-key',key);
  }
  return [...args,'--project',p.project,'--host-command-json',JSON.stringify(p.hostArgv),
    '--validation-command-json',JSON.stringify(p.validatorArgv),
    '--validation-timeout-seconds',String(p.validatorTimeout),
    '--timeout-seconds',String(p.turnTimeout),'--no-global-sync','--execute'];
}

/** A cursor stores only exact LoopX journal keys, not another result or spend ledger. */
export async function runCycle(p,{call=callCli,signal}={}){
  const deadline=Date.now()+p.maxSeconds*1000;
  const invoke=(args)=>call([...p.cliPrefix,...args],{cwd:p.project,signal,
    timeoutMs:Math.max(1,deadline-Date.now())});
  const preview=()=>invoke(['turn','plan',...decisionArgs(p,0),'--include-transaction-detail']);
  if(!p.execute){
    const value=await preview();
    return {status:value.ok&&value.route?.would_invoke_host===true?'ready':'blocked',
      execute:false,pi_started:false,turn_key:value.transaction?.turn_key??null};
  }
  const runtimeIndex=p.cliPrefix.indexOf('--runtime-root');
  if(runtimeIndex<0||!p.cliPrefix[runtimeIndex+1])throw new Error('Explicit LoopX runtime root required');
  await claimGoalOwner(p.cliPrefix[runtimeIndex+1],p.goalId,{owner:'legacy-cycle',goalId:p.goalId,agentId:p.agentId});
  let cursor={schema_version:'kernelcat.loopx-cursor/v1',plan_digest:p.planDigest,keys:[]};
  try {
    cursor=JSON.parse(await readFile(p.cursor,'utf8'));
    if(cursor.schema_version!=='kernelcat.loopx-cursor/v1'||cursor.plan_digest!==p.planDigest||
       !Array.isArray(cursor.keys)||cursor.keys.some(key=>!keyPattern.test(key)))throw new Error('Cursor identity mismatch');
  } catch(error){if(error.code!=='ENOENT')throw error;}
  const turns=[];
  for(let index=0;index<p.maxTurns;index++){
    if(signal?.aborted)return {status:'cancelled',turns};
    if(Date.now()>=deadline)return {status:'deadline',turns};
    const existing=cursor.keys[index];
    if(!existing){
      const plan=await invoke(['turn','plan',...decisionArgs(p,index),'--include-transaction-detail']);
      if(plan.ok!==true||plan.route?.would_invoke_host!==true)return {status:'blocked',turns};
      const key=plan.transaction?.turn_key;
      if(!keyPattern.test(key??''))throw new Error('Turn plan has no exact journal key');
      cursor.keys.push(key);await saveCursor(p.cursor,cursor);
    }
    // Resuming always addresses the original journal, not a newly computed plan.
    const value=await invoke(['turn','run-once',...executionArgs(p,index,existing)]);
    const actual=value.resume_turn_key;
    if(value.ok===true&&(!keyPattern.test(actual??'')||value._exitCode>0))
      throw new Error('Successful response lacks a valid Turn identity');
    if(keyPattern.test(actual??'')&&actual!==cursor.keys[index]){
      if(existing)throw new Error('Resume changed the recorded Turn identity');
      cursor.keys[index]=actual;await saveCursor(p.cursor,cursor);
    }
    turns.push({turn_key:cursor.keys[index],status:value.status??'unknown',
      result_kind:value.result_kind??null,replayed:value.replayed===true,
      validated:value.validation?.status==='passed',
      quota_spent:value.effects?.quota_spent===true,
      phases:value.receipt?.completed_phases??[]});
    if(value.ok!==true||value.status!=='committed'||value.receipt?.ok!==true)
      return {status:'stopped',turns,recovery_required:true};
    if(value.result_kind==='validated_completion')return {status:'completed',turns};
    if(value.result_kind!=='validated_progress')return {status:'stopped',turns};
  }
  return {status:'turn_limit',turns};
}

if(process.argv[1]&&import.meta.url===pathToFileURL(process.argv[1]).href){
  const controller=new AbortController();
  process.once('SIGINT',()=>controller.abort());process.once('SIGTERM',()=>controller.abort());
  try {
    let input='';for await(const part of process.stdin){input+=part;if(Buffer.byteLength(input)>1048576)throw new Error('Input limit');}
    const result=await runCycle(JSON.parse(input),{signal:controller.signal});
    process.stdout.write(JSON.stringify(result)+'\n');
    if(['stopped','deadline','cancelled'].includes(result.status))process.exitCode=3;
  } catch {
    process.stdout.write(JSON.stringify({status:'cycle_error',recovery_required:true,
      message:'Inspect the saved LoopX Turn keys and local journals before resuming.'})+'\n');process.exitCode=2;
  }
}
