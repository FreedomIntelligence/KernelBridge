import test from 'node:test';
import assert from 'node:assert/strict';
import * as fs from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import * as sdk from '../source/apps/agent/node_modules/@earendil-works/pi-coding-agent/dist/index.js';
import { workspaceTools } from '../source/apps/agent/src/harness/pi/workspace-tools.mjs';

async function fixture(run, { writable_paths = ['solution.py','nested/new.py'], readonly_paths = ['reference'] } = {}) {
  const base = await fs.mkdtemp(join(tmpdir(), 'kb-native-regression-'));
  const root = join(base, 'workspace');
  await fs.mkdir(join(root, 'reference'), { recursive: true });
  await fs.writeFile(join(root, 'reference/semantics.txt'), 'immutable reference');
  await fs.writeFile(join(root, 'solution.py'), 'x = 1\ny = 2\n');
  await fs.writeFile(join(base, 'private.txt'), 'EXTERNAL_SECRET_MARKER');
  let case_sensitive = true;
  try { case_sensitive = (await fs.stat(join(root, 'REFERENCE'))).ino !== (await fs.stat(join(root, 'reference'))).ino; }
  catch (error) { if (error.code !== 'ENOENT') throw error; }
  const tools = new Map(workspaceTools(sdk, {root,tools:['read','write','edit','ls','find','grep','bash'],
    writable_paths, readonly_paths, case_sensitive}).map(tool=>[tool.name,tool]));
  const call = (name, args, signal, update) => tools.get(name).execute('fixture', args, signal, update);
  try { await run({base, root, call, tools, case_sensitive}); } finally { await fs.rm(base,{recursive:true,force:true}); }
}

test('native edits, complete writes and line reads use the admitted workspace', () => fixture(async ({root,call}) => {
  await call('edit',{path:'solution.py',edits:[{oldText:'x = 1',newText:'x = 3'},{oldText:'y = 2',newText:'y = 4'}]});
  assert.equal(await fs.readFile(join(root,'solution.py'),'utf8'),'x = 3\ny = 4\n');
  const result=await call('read',{path:'solution.py',offset:2,limit:1});
  assert.match(result.content[0].text,/y = 4/);
  await call('write',{path:'nested/new.py',content:'${PKG_NAME} `literal`'});
  assert.equal(await fs.readFile(join(root,'nested/new.py'),'utf8'),'${PKG_NAME} `literal`');
}));

test('all file tools enforce scope, readonly paths and symlink boundaries', () => fixture(async ({base,root,call}) => {
  await fs.symlink(join(base,'private.txt'),join(root,'link.txt'));
  for(const name of ['read','write','edit','ls','find','grep']) {
    const args={path:'../private.txt',content:'bad',edits:[{oldText:'x',newText:'y'}],pattern:'SECRET'};
    await assert.rejects(call(name,args));
  }
  await assert.rejects(call('read',{path:'link.txt'}),/symlink/);
  await assert.rejects(call('write',{path:'reference/semantics.txt',content:'bad'}),/Read-only/);
  await assert.rejects(call('edit',{path:'reference/semantics.txt',edits:[{oldText:'immutable',newText:'changed'}]}),/Read-only/);
  await assert.rejects(call('write',{path:'unauthorized.py',content:'bad'}),/Read-only/);
  assert.equal(await fs.readFile(join(root,'reference/semantics.txt'),'utf8'),'immutable reference');
}));

test('native search cannot expose symlinks or controller-reserved files', () => fixture(async ({base,root,call}) => {
  await fs.symlink(join(base,'private.txt'),join(root,'link.txt'));
  await fs.mkdir(join(root,'.evaluations'));
  await fs.writeFile(join(root,'.evaluations/private.txt'),'EXTERNAL_SECRET_MARKER');
  const found=await call('find',{pattern:'*.py'});
  assert.match(found.content[0].text,/solution.py/);
  const grep=await call('grep',{pattern:'x ='});
  assert.match(grep.content[0].text,/solution.py:1: x = 1/);
  const secret=await call('grep',{pattern:'EXTERNAL_SECRET_MARKER'});
  assert.match(secret.content[0].text,/No matches/);
  const listing=await call('ls',{});
  assert.doesNotMatch(listing.content[0].text,/link.txt|evaluations/);
}));

test('bash runs in isolation and applies only an authorized complete diff', () => fixture(async ({base,root,call}) => {
  const result=await call('bash',{command:"printf 'x = 5\n' > solution.py; cat solution.py",timeout:5});
  assert.match(result.content[0].text,/x = 5/);
  assert.equal(await fs.readFile(join(root,'solution.py'),'utf8'),'x = 5\n');
  const denied = await call('bash',{command:"printf bad > reference/semantics.txt; printf bad > solution.py",timeout:5});
  assert.notEqual(denied.structuredContent.exit_code, 0);
  assert.equal(await fs.readFile(join(root,'solution.py'),'utf8'),'x = 5\n');
  assert.equal(await fs.readFile(join(root,'reference/semantics.txt'),'utf8'),'immutable reference');
  const escaped=await call('bash',{command:`cat '${join(base,'private.txt')}'`,timeout:5});
  assert.notEqual(escaped.structuredContent.exit_code,0);
  assert.doesNotMatch(escaped.content[0].text,/EXTERNAL_SECRET_MARKER/);
  const linked=await call('bash',{command:`ln '${join(base,'private.txt')}' leaked.txt && cat leaked.txt`,timeout:5});
  assert.notEqual(linked.structuredContent.exit_code,0);
  assert.doesNotMatch(linked.content[0].text,/EXTERNAL_SECRET_MARKER/);
  const outbound=await call('bash',{command:'curl --connect-timeout 1 --max-time 2 -sS https://example.com',timeout:5});
  assert.notEqual(outbound.structuredContent.exit_code,0);
}));

test('bash preserves executable permissions on an authorized file', () => fixture(async ({root,call}) => {
  await call('bash',{command:'chmod +x solution.py',timeout:5});
  assert.ok((await fs.stat(join(root,'solution.py'))).mode & 0o100);
  await call('edit',{path:'solution.py',edits:[{oldText:'x = 1',newText:'x = 7'}]});
  assert.ok((await fs.stat(join(root,'solution.py'))).mode & 0o100);
}));

test('bash cancellation and timeout do not commit a partial candidate', () => fixture(async ({root,call}) => {
  await assert.rejects(call('bash',{command:'printf bad > solution.py; sleep 3',timeout:0.05}),/timed out|timeout/i);
  assert.equal(await fs.readFile(join(root,'solution.py'),'utf8'),'x = 1\ny = 2\n');
}));

test('read truncation and missing edits preserve complete source', () => fixture(async ({root,call}) => {
  const content=Array.from({length:4000},(_,i)=>`line ${i}`).join('\n');
  await fs.writeFile(join(root,'solution.py'),content);
  const result=await call('read',{path:'solution.py'});
  assert.ok(result.content[0].text.length<content.length);
  assert.match(result.content[0].text,/offset|truncat|lines/i);
  await assert.rejects(call('edit',{path:'solution.py',edits:[{oldText:'missing text',newText:'bad'}]}));
  assert.equal(await fs.readFile(join(root,'solution.py'),'utf8'),content);
}));

test('bash detects concurrent edits before writeback', () => fixture(async ({root,call}) => {
  const running=call('bash',{command:'sleep 0.3; printf shell > solution.py',timeout:5});
  await new Promise(resolve=>setTimeout(resolve,150));
  await fs.writeFile(join(root,'solution.py'),'external editor');
  await assert.rejects(running,/changed during bash/);
  assert.equal(await fs.readFile(join(root,'solution.py'),'utf8'),'external editor');
}));

test('explicit cancellation stops the shell without candidate writeback', () => fixture(async ({root,call}) => {
  const controller=new AbortController();
  const running=call('bash',{command:'printf bad > solution.py; sleep 30',timeout:40},controller.signal);
  const timer=setTimeout(()=>controller.abort(),150);
  try { await assert.rejects(running,/abort|cancel/i); } finally { clearTimeout(timer); }
  assert.equal(await fs.readFile(join(root,'solution.py'),'utf8'),'x = 1\ny = 2\n');
}));

test('isolated shell has a writable private temporary directory', () => fixture(async ({call}) => {
  const result=await call('bash',{command:'p=$(mktemp "$TMPDIR/kb.XXXXXX"); echo temporary > "$p"; cat "$p"; rm "$p"',timeout:5});
  assert.match(result.content[0].text,/temporary/);
  assert.equal(result.structuredContent.exit_code,0);
}));

const scratchPolicy = { writable_paths: null, readonly_paths: ['reference', 'refs', 'AGENTS.md', 'docs/spec.md', '.kernelbridge/agents'] };

test('ordinary relative helper files persist across native tools and successful bash calls', () => fixture(async ({base, root, call, tools}) => {
  await call('write', { path: 'parts_head.txt', content: 'printf helper' });
  await call('edit', { path: 'parts_head.txt', edits: [{ oldText: 'helper', newText: 'reused_helper' }] });
  assert.match((await call('read', { path: 'parts_head.txt' })).content[0].text, /reused_helper/);
  assert.match((await call('find', { pattern: '*.txt' })).content[0].text, /parts_head/);
  assert.match((await call('grep', { pattern: 'reused_helper' })).content[0].text, /parts_head/);
  assert.match((await call('ls', {})).content[0].text, /parts_head/);
  const made = await call('bash', { command: 'sh ./parts_head.txt > result.txt', timeout: 5 });
  assert.equal(made.structuredContent.exit_code, 0);
  assert.equal(await fs.readFile(join(root, 'result.txt'), 'utf8'), 'reused_helper');
  const removed = await call('bash', { command: 'cat result.txt; rm result.txt', timeout: 5 });
  assert.match(removed.content[0].text, /reused_helper/);
  await assert.rejects(fs.stat(join(root, 'result.txt')), { code: 'ENOENT' });
  await assert.rejects(call('write', { path: join(base, 'head.py'), content: 'outside' }), /outside/);
  await assert.rejects(call('write', { path: '/tmp/head.py', content: 'outside' }), /outside/);
  assert.match(tools.get('write').description, /workspace-relative.*Direct writes cannot access host \/tmp/s);
}, scratchPolicy));

test('unremoved TMPDIR files are discarded while workspace helpers persist', () => fixture(async ({root, call}) => {
  const first = await call('bash', { command: 'printf temporary > "$TMPDIR/unremoved.bin"; printf "%s" "$TMPDIR" > temp-path.txt; printf keep > helper.txt', timeout: 5 });
  assert.equal(first.structuredContent.exit_code, 0);
  const temporary = await fs.readFile(join(root, 'temp-path.txt'), 'utf8');
  assert.notEqual(temporary, root);
  if (process.platform === 'darwin') await assert.rejects(fs.stat(temporary), { code: 'ENOENT' });
  else assert.equal(temporary, '/tmp', 'Linux TMPDIR is a private namespace mount');
  await assert.rejects(fs.stat(join(root, 'unremoved.bin')), { code: 'ENOENT' });
  const second = await call('bash', { command: 'test ! -e "$TMPDIR/unremoved.bin"; cat helper.txt', timeout: 5 });
  assert.equal(second.structuredContent.exit_code, 0);
  assert.match(second.content[0].text, /keep/);
}, scratchPolicy));

test('Python helpers and generated bytecode survive ordinary workspace calls', () => fixture(async ({root, call}) => {
  await call('write', { path: 'helper.py', content: 'VALUE = 37\n' });
  const result = await call('bash', { command: "python3 -c 'import helper; print(helper.VALUE)'", timeout: 10 });
  assert.equal(result.structuredContent.exit_code, 0, result.content[0].text);
  assert.match(result.content[0].text, /37/);
  const bytecode = (await fs.readdir(join(root, '__pycache__'))).find(name => name.endsWith('.pyc'));
  assert(bytecode, 'an ordinary Python import generates persistent binary scratch');
  assert((await fs.readFile(join(root, '__pycache__', bytecode))).length > 0);
  assert.match((await call('read', { path: 'helper.py' })).content[0].text, /VALUE = 37/);
  const reused = await call('bash', { command: "python3 -c 'import helper; print(helper.VALUE)'", timeout: 10 });
  assert.equal(reused.structuredContent.exit_code, 0);
  assert.match(reused.content[0].text, /37/);
}, scratchPolicy));

test('readonly roots and their ancestors stay protected with broad helper permissions', () => fixture(async ({root, call}) => {
  await fs.writeFile(join(root, 'AGENTS.md'), 'immutable instructions');
  await fs.mkdir(join(root, 'docs'));
  await fs.writeFile(join(root, 'docs/spec.md'), 'immutable specification');
  for (const path of ['AGENTS.md', 'docs/spec.md', 'docs', 'refs/new.py', '.kernelbridge', '.kernelbridge/agents/role.md', '.git/config']) {
    await assert.rejects(call('write', { path, content: 'bad' }), /Read-only|outside/);
  }
  for (const command of ['mkdir refs', 'printf bad > AGENTS.md', 'mv docs moved-docs', 'mkdir .git', 'mkdir .kernelbridge/agents']) {
    const result = await call('bash', { command: 'printf partial > partial.txt; ' + command, timeout: 5 });
    assert.notEqual(result.structuredContent.exit_code, 0, command);
    await assert.rejects(fs.stat(join(root, 'partial.txt')), { code: 'ENOENT' });
  }
  assert.equal(await fs.readFile(join(root, 'AGENTS.md'), 'utf8'), 'immutable instructions');
  assert.equal(await fs.readFile(join(root, 'docs/spec.md'), 'utf8'), 'immutable specification');
  await assert.rejects(fs.stat(join(root, 'refs')), { code: 'ENOENT' });
  await call('write', { path: 'docs/helper.py', content: 'allowed sibling' });
  assert.equal(await fs.readFile(join(root, 'docs/helper.py'), 'utf8'), 'allowed sibling');
}, scratchPolicy));

test('case-insensitive filesystem aliases cannot bypass native or shell protection', async t => fixture(async ({root, call, case_sensitive}) => {
  if (case_sensitive) { t.skip('requires an actually case-insensitive filesystem'); return; }
  await assert.rejects(call('write', { path: 'REFS/new.py', content: 'bad' }), /Read-only/);
  await fs.writeFile(join(root, 'AGENTS.md'), 'immutable instructions');
  await fs.mkdir(join(root, 'refs'));
  await fs.writeFile(join(root, 'refs/oracle.py'), 'immutable oracle');
  for (const path of ['agents.md', 'REFS/oracle.py', 'REFS/new.py', '.GIT/config', '.KERNELAGENT/private', '.KERNELBRIDGE/AGENTS/role.md']) {
    await assert.rejects(call('write', { path, content: 'bad' }), /Read-only|outside|alias/);
  }
  await assert.rejects(call('edit', { path: 'agents.md', edits: [{ oldText: 'immutable', newText: 'changed' }] }), /Read-only|alias/);
  await call('write', { path: 'helper.py', content: 'canonical helper' });
  await assert.rejects(call('write', { path: 'HELPER.py', content: 'alias overwrite' }), /alias/);
  assert.equal(await fs.readFile(join(root, 'helper.py'), 'utf8'), 'canonical helper');
  for (const command of ['printf bad > agents.md', 'mkdir .GIT', 'mkdir .KERNELBRIDGE/AGENTS', 'mv REFS moved-refs']) {
    const result = await call('bash', { command, timeout: 5 });
    assert.notEqual(result.structuredContent.exit_code, 0, command);
  }
  assert.equal(await fs.readFile(join(root, 'refs/oracle.py'), 'utf8'), 'immutable oracle');
  assert.equal(await fs.readFile(join(root, 'AGENTS.md'), 'utf8'), 'immutable instructions');
}, scratchPolicy));

test('case-sensitive filesystems retain genuinely distinct native names', async t => fixture(async ({root, call, case_sensitive}) => {
  if (!case_sensitive) { t.skip('requires an actually case-sensitive filesystem'); return; }
  await call('write', { path: 'REFS/helper.py', content: 'distinct allowed path' });
  assert.equal(await fs.readFile(join(root, 'REFS/helper.py'), 'utf8'), 'distinct allowed path');
}, scratchPolicy));
