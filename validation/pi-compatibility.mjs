/** Offline compatibility checks for the exact Pi runtime used by this delivery. */
import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, readFile, writeFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { withPreparedAgent } from '../source/apps/agent/src/harness/pi/sdk.mjs';
import { controlledContext } from '../source/apps/agent/src/loopx/context-control.ts';
const sdk = await import('../source/apps/agent/node_modules/@earendil-works/pi-coding-agent/dist/index.js');

test('Pi and plugin pins match installed packages', async () => {
  const pkg = JSON.parse(await readFile(new URL('../source/apps/agent/package.json', import.meta.url), 'utf8'));
  for (const [name, expected] of Object.entries(pkg.dependencies)) {
    const installed = JSON.parse(await readFile(new URL(`../source/apps/agent/node_modules/${name}/package.json`, import.meta.url), 'utf8'));
    assert.equal(installed.version, expected, name);
  }
});

test('real Pi native session executes CodeMode/MCP and checkpoints with the controlled host', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'pi15-harness-check-'));
  const methods = []; const results = []; let turns = 0; let leafCalls = 0;
  const read = { name: 'read_file', description: 'Read a test fixture', parameters: {type:'object',properties:{path:{type:'string'}},required:['path'],additionalProperties:false}, credits:1 };
  const client = { async call(method, params = {}) {
    methods.push(method);
    if (method === 'hello') return {task_id:'compat',goal:'Use the supplied test fixture.',tools:[read],execution_mode:'sequential',native_harness:{schema_version:'kernelcat.pi-harness/v1',adapter:'pi@1.5.0',native_tools:['codemode','tool_search'],mcp_server:'kernelcat'}};
    if (method === 'model_turn') {
      assert.equal(params.harness.adapter, 'pi@1.5.0');
      if (turns++ === 0) return {proposal_id:'p1',text:'',parallel:false,calls:[{id:'c1',name:'codemode',arguments:{code:'const results = await Promise.allSettled([tools.mcp__kernelcat__read_file({path:"fixture.txt"}), tools.mcp__kernelcat__read_file({path:"fixture.txt"})]); text(results);'}}]};
      return {proposal_id:'p2',text:'Fixture inspected.',parallel:false,calls:[]};
    }
    if (method === 'native_begin') return {permit:'c1'};
    if (method === 'native_call') return {action_id:`c1/nested/${leafCalls++}`,outcome:{status:'completed',data:{text:'fixture data'}}};
    if (method === 'native_settle') {results.push(params);return {action_id:'c1',outcome:{status:params.status,data:{result:params.result}}};}
    if (method === 'end_turn') return {stop:params.proposal_id==='p2',result:params.proposal_id==='p2'?{status:'finished'}:null};
    if (method === 'complete') return {status:'finished'};
    throw new Error(`Unexpected RPC ${method}`);
  }};
  try {
    const result = await withPreparedAgent(run => run(controlledContext(client, directory)));
    assert.equal(result.status, 'finished');
    assert.equal(turns, 2);
    assert.equal(methods.filter(x => x==='native_call').length, 2);
    assert.equal(results.length, 1);
    assert.equal(results[0].status, 'completed');
    assert.equal(results[0].result.isError, false);
  } finally { await rm(directory, {recursive:true,force:true}); }
});

test('Pi native read/write/edit preserve generated source without CodeMode interpolation', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'pi15-tools-check-'));
  const content = 'set(PKG_NAME "fixture")\nadd_library(${PKG_NAME} SHARED source.cpp)\n# literal `backtick`\n';
  try {
    await sdk.createWriteTool(directory).execute('w1', {path:'CMakeLists.txt',content});
    assert.equal(await readFile(join(directory,'CMakeLists.txt'),'utf8'), content);
    await sdk.createEditTool(directory).execute('e1', {path:'CMakeLists.txt',edits:[{oldText:'"fixture"',newText:'"kernel"'}]});
    assert.equal(await readFile(join(directory,'CMakeLists.txt'),'utf8'), content.replace('"fixture"','"kernel"'));
    const result = await sdk.createReadTool(directory).execute('r1', {path:'CMakeLists.txt'});
    assert.match(result.content.map(x=>x.text??'').join('\n'), /\$\{PKG_NAME\}/);
  } finally { await rm(directory,{recursive:true,force:true}); }
});

test('Pi write operations provide a policy enforcement seam', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'pi15-policy-check-'));
  try {
    await writeFile(join(directory,'reference.txt'),'immutable');
    const tool = sdk.createWriteTool(directory, {operations:{
      mkdir:async()=>{},
      writeFile:async(path,content)=>{if(path!==join(directory,'solution.py'))throw new Error('read-only source');await writeFile(path,content);},
    }});
    await assert.rejects(tool.execute('w1',{path:'reference.txt',content:'modified'}),/read-only/);
    assert.equal(await readFile(join(directory,'reference.txt'),'utf8'),'immutable');
    await tool.execute('w2',{path:'solution.py',content:'candidate'});
    assert.equal(await readFile(join(directory,'solution.py'),'utf8'),'candidate');
  } finally { await rm(directory,{recursive:true,force:true}); }
});
