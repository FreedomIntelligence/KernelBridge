import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, writeFile, chmod, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { codegenSettings } from '../source/apps/agent/src/application/codegen.mjs';
import { goalSummary, statePresentation, installProductUI } from '../source/apps/agent/src/harness/pi/product-ui.mjs';
import { InputQueue } from '../source/apps/agent/src/application/input-queue.mjs';
import { loadCodegenCredentials } from '../source/apps/agent/src/application/credentials.mjs';

test('codegen settings preserve absent limits and require explicit model selection', async () => {
  const root = await mkdtemp(join(tmpdir(), 'kb-settings-'));
  try {
    const config = join(root, 'config.json');
    await writeFile(config, JSON.stringify({ providers: { primary: { provider: 'anthropic', model: 'fixture', api_key_env: 'KB_MISSING_TEST_KEY' } } }));
    const settings = await codegenSettings({ cwd: root, config });
    assert.equal(settings.credits, null);
    assert.equal(settings.config.policy, undefined);
    assert.equal(settings.providers.primary.base_url, 'https://api.anthropic.com');
    assert.equal(settings.providers.primary.wire_options.native_turns, true);
    assert.match(settings.problem, /KB_MISSING_TEST_KEY/);
    assert.equal((await codegenSettings({ cwd: root, config, budget: '27' })).credits, 27);
    await assert.rejects(codegenSettings({ cwd: root, config, budget: '0' }), /正整数/);
    const switched = await codegenSettings({ cwd: root, config, provider: 'openai', model: 'second' });
    assert.equal(switched.providers.primary.provider, 'openai');
    assert.equal(switched.providers.primary.base_url, 'https://api.openai.com/v1');
    assert.equal(switched.providers.primary.api_key_env, 'OPENAI_API_KEY');
    await writeFile(config, JSON.stringify({providers:{ds:{model:'fixture-flash'},'ds-pro':{model:'fixture-pro'}},policy:{routes:['ds','ds-pro']}}));
    const alias = await codegenSettings({cwd:root,config,model:'ds-pro'});
    assert.equal(alias.model,'fixture-pro');
    assert.deepEqual(alias.routes,['ds-pro','ds']);
    await writeFile(config, JSON.stringify({ evaluator: { argv: ['never-run'] } }));
    await assert.rejects(codegenSettings({ cwd: root, config }), /显式/);
  } finally { await rm(root, { recursive: true, force: true }); }
});

test('local credentials are private, environment-overridable and separate from provider settings', async () => {
  const root = await mkdtemp(join(tmpdir(),'kb-auth-'));
  const previousHome = process.env.KERNELBRIDGE_HOME, previousKey = process.env.KB_LOCAL_TEST_KEY;
  const providers = {ds:{api_key_env:'KB_LOCAL_TEST_KEY'}};
  try {
    process.env.KERNELBRIDGE_HOME = root; delete process.env.KB_LOCAL_TEST_KEY;
    const path = join(root,'credentials.json');
    await writeFile(path,JSON.stringify({KB_LOCAL_TEST_KEY:'fixture-local-key'}),{mode:0o600});
    await loadCodegenCredentials(providers);
    assert.equal(process.env.KB_LOCAL_TEST_KEY,'fixture-local-key');
    assert.deepEqual(providers,{ds:{api_key_env:'KB_LOCAL_TEST_KEY'}});
    process.env.KB_LOCAL_TEST_KEY = 'explicit-env';
    await loadCodegenCredentials(providers);
    assert.equal(process.env.KB_LOCAL_TEST_KEY,'explicit-env');
    delete process.env.KB_LOCAL_TEST_KEY;
    await chmod(path,0o644);
    await assert.rejects(loadCodegenCredentials(providers),/0600/);
  } finally {
    if(previousHome===undefined)delete process.env.KERNELBRIDGE_HOME;else process.env.KERNELBRIDGE_HOME=previousHome;
    if(previousKey===undefined)delete process.env.KB_LOCAL_TEST_KEY;else process.env.KB_LOCAL_TEST_KEY=previousKey;
    await rm(root,{recursive:true,force:true});
  }
});

test('ordinary codegen UI contains no fake budget or trial status', () => {
  const state = { workspace_mode: 'project', cwd: '/project', model: 'fixture', budget: { limit: null, spent: 501, reserved: 0 },
    inputs: {}, trials: [], goal: null, task_version: 1, pending_effect_ids: [] };
  let footer;
  installProductUI({ ui: { setHeader() {}, setFooter(factory) { footer = factory(); } } }, () => state);
  for (const text of [goalSummary(state), statePresentation(state), footer.render(40).join('\n')]) {
    assert.doesNotMatch(text, /预算|credits|Trial|501|200|controlled/);
  }
  assert.match(footer.render(40).join('\n'), /fixture.*等待输入/);
  state.budget.limit = 900;
  assert.match(footer.render(40).join('\n'), /501\/900/);
});

test('native input cannot be consumed again through a racing queued delivery', () => {
  const inputs = new InputQueue({});
  const original = { input_id: 'first', text: 'same text', status: 'queued' };
  inputs.enqueue(original);
  assert.equal(inputs.consume('same text').input_id, 'first');
  inputs.enqueue(original); // next() raced the native handler before applied acknowledgement
  assert.throws(() => inputs.consume('same text'), /no durable host receipt/);
  inputs.enqueue({ ...original, input_id: 'second' });
  assert.equal(inputs.consume('same text').input_id, 'second', 'a later deliberate identical prompt remains valid');
});
