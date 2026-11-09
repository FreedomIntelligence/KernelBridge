import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {withPreparedAgent} from '../source/apps/agent/src/harness/pi/sdk.mjs';
import {controlledContext} from '../source/apps/agent/src/loopx/context-control.ts';

test('default evaluate bypasses the MCP deadline and retains its original receipt', async()=>{
  const directory=await mkdtemp(join(tmpdir(),'kb-business-check-'));
  let turns=0; let completed=false; const methods=[];
  const client={async call(method,params={}){
    methods.push(method);
    if(method==='hello')return {task_id:'business',goal:'Evaluate the candidate.',execution_mode:'sequential',
      tools:[{name:'evaluate',description:'Run the configured NPU evaluator',parameters:{type:'object',properties:{},additionalProperties:false},credits:2}],
      native_harness:{schema_version:'kernelcat.pi-harness/v1',adapter:'pi@1.5.0',native_tools:[],mcp_server:'kernelcat',timeout:0.015}};
    if(method==='model_turn'){
      const names=params.harness.tools.map(t=>t.name);
      assert.ok(names.includes('evaluate'));
      assert.ok(!names.includes('codemode'));
      assert.ok(!names.some(n=>n.startsWith('mcp__')));
      if(turns++===0)return {proposal_id:'p1',text:'',parallel:false,calls:[{id:'eval-1',name:'evaluate',arguments:{}}]};
      assert.ok(completed);
      const messages=JSON.stringify(params.harness.messages);
      assert.match(messages,/original-evaluation-receipt/);
      return {proposal_id:'p2',text:'Evaluated.',parallel:false,calls:[]};
    }
    if(method==='tool'){
      await new Promise(resolve=>setTimeout(resolve,80));
      completed=true;
      return {action_id:'original-evaluation-receipt',outcome:{status:'completed',data:{correctness:true}}};
    }
    if(method==='end_turn')return {stop:params.proposal_id==='p2',result:null};
    if(method==='complete')return {status:'finished'};
    throw new Error(`Unexpected RPC ${method}`);
  }};
  try{
    assert.equal((await withPreparedAgent(run=>run(controlledContext(client,directory)))).status,'finished');
    assert.equal(methods.filter(m=>m==='tool').length,1);
    assert.ok(!methods.some(m=>m.startsWith('native_')));
  } finally {await rm(directory,{recursive:true,force:true});}
});
