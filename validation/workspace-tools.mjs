import test from 'node:test';
import assert from 'node:assert/strict';
import * as fs from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import * as sdk from '../source/apps/agent/node_modules/@earendil-works/pi-coding-agent/dist/index.js';
import { workspaceTools } from '../source/apps/agent/src/harness/pi/workspace-tools.mjs';

async function fixture(run) {
  const base = await fs.mkdtemp(join(tmpdir(), 'kb-native-regression-'));
  const root = join(base, 'workspace');
  await fs.mkdir(join(root, 'reference'), { recursive: true });
  await fs.writeFile(join(root, 'reference/semantics.txt'), 'immutable reference');
  await fs.writeFile(join(root, 'solution.py'), 'x = 1\ny = 2\n');
  await fs.writeFile(join(base, 'private.txt'), 'EXTERNAL_SECRET_MARKER');
  const tools = new Map(workspaceTools(sdk, {root,tools:['read','write','edit','ls','find','grep','bash'],
    writable_paths:['solution.py','nested/new.py'],readonly_paths:['reference']}).map(tool=>[tool.name,tool]));
  const call = (name, args) => tools.get(name).execute('fixture', args);
  try { await run({base, root, call}); } finally { await fs.rm(base,{recursive:true,force:true}); }
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
  await assert.rejects(call('bash',{command:"printf bad > reference/semantics.txt; printf bad > solution.py",timeout:5}),/Read-only/);
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
