import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { projectReplayContext, DEFAULT_REPLAY_CLEANUP_OPTIONS as defaults } from '../source/apps/agent/src/replay/context-cleanup.ts';
import { contextDigest, immutableReplayReferenceText } from '../source/apps/agent/src/replay/context-contract.ts';
import { createContextControl, retentionReserve } from '../source/apps/agent/src/loopx/context-control.ts';
import { controlledModels } from '../source/apps/agent/src/loopx/model-control.ts';
import { encodedBytes } from '../source/apps/agent/src/context/compiler.ts';
import { applyContextManifest } from '../source/apps/agent/src/replay/context-manifest.ts';
import { canonicalKey } from '../source/apps/agent/src/run/scoring/canonical.ts';

const text = value => [{ type:'text', text:value }];
const answer = value => ({role:'assistant',content:text(value)});
function exchange(id, size = 12000, {status='completed', isError=false, name='read'} = {}) {
  const result = {role:'toolResult',toolCallId:id,toolName:name,isError,content:text('λ'.repeat(size/2))};
  return { messages:[{role:'assistant',content:[{type:'toolCall',id,name,arguments:{path:'reference.txt'}}]},result],
    proof:{status,reference:{uri:`read-receipt:${id}`,digest:contextDigest(result.content)}} };
}
function fixture(...exchanges) {
  const messages=[{role:'user',content:'Inspect the declared sources.'}], metadata=[];
  for (const item of exchanges) {const index=messages.length+1;messages.push(...item.messages);metadata.push({index,...item.proof});}
  messages.push(answer('One subsequent turn.'),answer('A second subsequent turn.'));
  return {messages,metadata};
}
function source(limit=2097152) { return { task:{id:'task',goal:'Inspect reference.'},skills:[],policy:{context_bytes:limit},budget:{limit:null,spent:0,reserved:0},history:[],
  projection:{version:'test',history_omitted:0,authority:'journal',context_byte_limit:limit,latest_action_id:null}}; }
function harness(messages, system_prompt='Coding instructions.') {return {schema_version:'kernelcat.pi-harness/v1',adapter:'pi@1.5.0',system_prompt,messages,tools:[]};}
async function control(f, run) {
  const dir=await mkdtemp(join(process.env.KERNELBRIDGE_TEST_TMP ?? tmpdir(),'kb-retention-'));
  try {const owner=createContextControl(dir);const ref=await owner.wrap({call(){throw Error('Unexpected external RPC');}}).call('context_boundary',f);return await run(owner,ref);}
  finally {await rm(dir,{recursive:true,force:true});}
}
function pressure(f, target=1) {return projectReplayContext(f.messages,defaults,f.metadata,{inputBytes:encodedBytes(f.messages),targetBytes:target});}

test('age alone preserves a full successful receipt over many assistant turns',()=>{
 const f=fixture(exchange('a'));f.messages.push(...Array.from({length:20},()=>answer('Inspecting.')));
 const original=structuredClone(f);const p=projectReplayContext(f.messages,defaults,f.metadata);
 assert.deepEqual(p.messages,f.messages);assert.equal(p.report.changed,false);assert.equal(p.report.inputDigest,p.report.outputDigest);
 assert.equal(p.report.decisions[2].reason,'within_budget');assert.deepEqual(f,original);
});

test('explicit byte pressure replaces only enough eligible results in oldest order',()=>{
 const f=fixture(exchange('a'),exchange('b'),exchange('c'));
 const bytes=encodedBytes(f.messages);const p=projectReplayContext(f.messages,defaults,f.metadata,{inputBytes:bytes,targetBytes:bytes-1000});
 assert.match(p.messages[2].content[0].text,/read_receipt/);assert.deepEqual(p.messages[4],f.messages[4]);assert.deepEqual(p.messages[6],f.messages[6]);
 assert.equal(p.report.decisions.filter(d=>d.action==='replace').length,1);assert.equal(p.report.outputDigest,contextDigest(p.messages));
});

test('8 KiB eligibility is exact and independent from pressure trigger',()=>{
 const item=exchange('threshold',0);const wrapper=Buffer.byteLength(JSON.stringify(text('')));
 item.messages[1].content=text('x'.repeat(8192-wrapper));item.proof.reference.digest=contextDigest(item.messages[1].content);
 const f=fixture(item);assert.equal(Buffer.byteLength(JSON.stringify(f.messages[2].content)),8192);
 assert.equal(pressure(f).report.decisions[2].action,'replace');
 f.messages[2].content[0].text=f.messages[2].content[0].text.slice(1);f.metadata[0].reference.digest=contextDigest(f.messages[2].content);
 assert.equal(pressure(f).report.decisions[2].reason,'short_result');
});

test('unknown failed and unproven outputs remain verbatim under pressure',()=>{
 for (const status of ['unknown','failed','completed']) {
  const f=fixture(exchange(status,12000,{status,isError:status==='completed'}));
  assert.deepEqual(pressure(f).messages,f.messages);assert.equal(pressure(f).report.decisions[2].reason,'unproven_or_unsuccessful');
 }
 const f=fixture(exchange('missing'));f.metadata=[];assert.deepEqual(pressure(f).messages,f.messages);
});

test('a mixed or pending exchange and the two recent assistant turns are protected',()=>{
 const f=fixture(exchange('a'),exchange('b',12000,{status:'failed'}));
 // One assistant batch has both results; a failed member protects the success too.
 f.messages[1].content.push(f.messages[3].content[0]);f.messages.splice(3,1);f.metadata[1].index--;
 assert.deepEqual(pressure(f).messages,f.messages);
 const pending=fixture(exchange('p'));pending.messages[1].content.push({type:'toolCall',id:'missing',name:'read',arguments:{}});
 assert.equal(pressure(pending).report.decisions[2].reason,'unpaired_or_pending');
 const recent=fixture(exchange('r'));recent.messages.pop();assert.equal(pressure(recent).report.decisions[2].reason,'recent');
});

test('immutable reference anchors and conflicting original identities retain their protection',()=>{
 const f=fixture(exchange('anchor'),exchange('target'));
 f.messages[2].content=text(immutableReplayReferenceText(f.metadata[1].reference));f.metadata[0].reference.digest=contextDigest(f.messages[2].content);
 f.metadata[1].reference.contextIndex=2;
 assert.equal(pressure(f).report.decisions[2].reason,'reference_anchor');
 const bad=fixture(exchange('duplicate'),exchange('duplicate'));
 bad.messages[4].content=text('mutated');assert.throws(()=>projectReplayContext(bad.messages,defaults,bad.metadata),/Conflicting replay result/);
});

test('owner boundary retains content and rejects a mutated or unrelated request prefix',async()=>{
 const f=fixture(exchange('original'));
 await control(f,async(owner,ref)=>{
  const applied=applyContextManifest(ref,f.messages);assert.deepEqual(applied.messages,f.messages);
  const h=harness(f.messages);assert.deepEqual(owner.project(h,source()).messages,f.messages);
  const changed=structuredClone(h);changed.messages[2].content=text('forged');assert.throws(()=>owner.project(changed,source()),/trusted original replay prefix/);
  assert.throws(()=>owner.project(harness([{role:'user',content:'Different session'}]),source()),/trusted original replay prefix/);
 });
});

test('measured owner source tools and UTF-8 envelope induce pressure despite small history',async()=>{
 const f=fixture(exchange('a'),exchange('b'));
 await control(f,async owner=>{
  const h=harness(f.messages,'中文'.repeat(6000));h.tools=[{name:'read',description:'工具'.repeat(1500),parameters:{type:'object'}}];
  const limit=encodedBytes(h)-5000;
  assert.ok(encodedBytes(f.messages)<limit-retentionReserve(limit));
  const p=owner.project(h,source(limit));assert.ok(encodedBytes(p)<=limit);assert.match(p.messages[2].content[0].text,/read_receipt/);
  assert.equal(p.system_prompt,h.system_prompt);assert.deepEqual(p.tools,h.tools);assert.deepEqual(h.messages,f.messages);
 });
});

test('protected overflow fails before dispatch and new suffix has no fabricated success metadata',async()=>{
 const f=fixture(exchange('u',12000,{status:'unknown'}));
 await control(f,async owner=>{
  assert.throws(()=>owner.project(harness(f.messages),source(4096)),/Protected model input/);
  const suffix=exchange('new',12000).messages;const h=harness([...f.messages,...suffix]);
  assert.deepEqual(owner.project(h,source()).messages,h.messages);
 });
});

test('actual controlledModels fits after source composition and refreshes on known fallback',async()=>{
 const f=fixture(exchange('old'),exchange('other'));const first=source(70000);const second=source(70000);second.task.note='x'.repeat(55000);second.budget.spent=1;
 await control(f,async owner=>{
  const sent=[];const base={async call(method,params={}){
   if(method==='hello')return {native_harness:{}};
   if(method==='prepare_turn')return {context_source:first,routes:['primary','fallback']};
   if(method==='model'){sent.push(structuredClone(params));return sent.length===1?{failed:true,context_source:second}:{text:'done',calls:[]};}
   throw Error(method);
  }};
  const rpc=owner.wrap(controlledModels(base,owner.project));await rpc.call('hello');
  await rpc.call('model_turn',{harness:harness(f.messages)});
  assert.equal(sent.length,2);assert.deepEqual(sent[0].harness.messages,f.messages);assert.match(sent[1].harness.messages[2].content[0].text,/read_receipt/);
  assert.ok(encodedBytes(sent[1].harness)<=70000);assert.ok(sent[1].harness.system_prompt.includes(canonicalKey(sent[1].context)));
  assert.deepEqual(first.budget,{limit:null,spent:0,reserved:0});
  await assert.rejects(rpc.call('model',{}),/protected model decision/);
 });
});

test('duplicate deliveries retain the original one and adjust the boundary metadata',async()=>{
 const f=fixture(exchange('a'),exchange('b'));f.messages.splice(3,0,structuredClone(f.messages[2]));f.metadata[1].index++;
 f.metadata.push({...f.metadata[0],index:3});f.metadata.sort((a,b)=>a.index-b.index);
 await control(f,async(owner,ref)=>{
  const p=applyContextManifest(ref,f.messages);assert.equal(p.messages.length,f.messages.length-1);
  assert.deepEqual(owner.project(harness(p.messages),source()).messages,p.messages);
 });
});

test('original trial replay retains all restored successful content at its original declared limit',
 {skip:!process.env.KERNELBRIDGE_RETENTION_REPLAY},async()=>{
  const f=JSON.parse(await readFile(process.env.KERNELBRIDGE_RETENTION_REPLAY,'utf8'));
  await control({messages:f.harness.messages,metadata:f.metadata},async owner=>{
   const selected=owner.project(f.harness,f.source);assert.deepEqual(selected,f.harness);assert.ok(encodedBytes(selected)<f.source.policy.context_bytes/10);
  });
 });

const nativeSdk = await import('../source/apps/agent/node_modules/@earendil-works/pi-coding-agent/dist/index.js');
const {projectHarness} = await import('../source/apps/agent/src/harness/pi/input.ts');
const {createReceiptContextBinding} = await import('../source/apps/agent/src/replay/receipt-context.ts');
const nativeInput = messages => projectHarness({messages:[{role:'system',content:'Fixture system instructions.',timestamp:0},...nativeSdk.convertToLlm(messages)]});

test('native shell custom and fork/compact messages use Pi conversion and preserve receipt indexes',async()=>{
 const f=fixture(exchange('visible-receipt'),exchange('second-receipt'));
 const extras=[
  {role:'bashExecution',command:'printf visible',output:'visible',exitCode:0,cancelled:false,truncated:false,timestamp:1},
  {role:'bashExecution',command:'printf hidden',output:'hidden',exitCode:0,cancelled:false,truncated:false,excludeFromContext:true,timestamp:2},
  {role:'custom',customType:'extension-note',content:'Model-visible custom 中文',display:false,timestamp:3},
  {role:'branchSummary',summary:'Original fork summary',fromId:'parent-entry',timestamp:4},
  {role:'compactionSummary',summary:'Original compact summary',tokensBefore:12000,timestamp:5},
 ];
 f.messages.unshift(...extras);f.metadata=f.metadata.map(m=>({...m,index:m.index+extras.length}));
 const actual=nativeInput(f.messages);
 await control(f,async owner=>{
  assert.deepEqual(owner.project(actual,source()).messages,actual.messages);
  assert.match(JSON.stringify(actual.messages),/Ran `printf visible`/);
  assert.ok(!JSON.stringify(actual.messages).includes('printf hidden'));
  for(const retained of ['Model-visible custom 中文','Original fork summary','Original compact summary']) assert.ok(JSON.stringify(actual.messages).includes(retained));
  const selected=owner.project(actual,source(encodedBytes(actual)-1000));
  const result=selected.messages.find(m=>m.role==='toolResult'&&m.toolCallId==='visible-receipt');
  assert.equal(result.content[0].text,immutableReplayReferenceText(f.metadata[0].reference));
  assert.deepEqual(selected.messages.slice(0,4),actual.messages.slice(0,4));
  const mutation=structuredClone(actual);mutation.messages[2].content[0].text='changed custom note';
  assert.throws(()=>owner.project(mutation,source()),/trusted original replay prefix/);
 });
});

test('native conversion handles excluded shell without shifting a reference anchor',async()=>{
 const f=fixture(exchange('anchor'),exchange('target'));
 f.messages[2].content=text(immutableReplayReferenceText(f.metadata[1].reference));f.metadata[0].reference.digest=contextDigest(f.messages[2].content);
 f.metadata[1].reference.contextIndex=2;
 f.messages.unshift({role:'bashExecution',command:'ignored',output:'',exitCode:0,cancelled:false,truncated:false,excludeFromContext:true,timestamp:1});
 f.metadata=f.metadata.map(m=>({...m,index:m.index+1,reference:{...m.reference,...(m.reference.contextIndex===undefined?{}:{contextIndex:m.reference.contextIndex+1})}}));
 await control(f,async owner=>{
  const actual=nativeInput(f.messages);
  const selected=owner.project(actual,source(encodedBytes(actual)-1000));
  assert.deepEqual(selected.messages[2],actual.messages[2]);
  assert.equal(selected.messages[4].content[0].text,immutableReplayReferenceText(f.metadata[1].reference));
 });
});

test('original failed PTY native session replays its real shell message and trusted receipts',
 {skip:!process.env.KERNELBRIDGE_RETENTION_NATIVE_REPLAY},async()=>{
  const entries=nativeSdk.parseSessionEntries(await readFile(process.env.KERNELBRIDGE_RETENTION_NATIVE_REPLAY,'utf8')).filter(e=>e.type!=='session');
  const messages=nativeSdk.buildSessionContext(entries).messages.filter(m=>m.role!=='system');
  assert.ok(messages.some(m=>m.role==='bashExecution' && !Object.hasOwn(m,'content')));
  const receipts=createReceiptContextBinding();receipts.bind(()=>{},entries);
  const metadata=receipts.metadata(messages,entries);
  await control({messages,metadata},async owner=>{
   const actual=nativeInput(messages);
   assert.deepEqual(owner.project(actual,source()).messages,actual.messages);
   assert.match(JSON.stringify(actual.messages),/NATIVE_SHELL/);
  });
 });
