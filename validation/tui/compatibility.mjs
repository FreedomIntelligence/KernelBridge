import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { InputQueue } from '../../source/apps/agent/src/application/input-queue.mjs';
import { loadInteractiveMode } from '../../source/apps/agent/src/harness/pi/interactive-patch.mjs';
import { createHostActions } from '../../source/apps/agent/src/harness/pi/host-actions.mjs';
import { KernelCat } from '../../source/apps/agent/src/sdk/index.ts';

test('pinned InteractiveMode loads the exact reviewed static patch', async () => {
  const result = await loadInteractiveMode();
  assert.equal(typeof result.InteractiveMode, 'function');
  const path = new URL('../../source/apps/agent/node_modules/@earendil-works/pi-coding-agent/dist/modes/interactive/interactive-mode.js', import.meta.url);
  const source = await readFile(path, 'utf8');
  const commands = [...new Set([...source.matchAll(/text === "(\/[^" ]+)"/g)].map(match => match[1]))].sort();
  assert.deepEqual(commands, result.metadata.commands);
  const { BUILTIN_SLASH_COMMANDS } = await import('../../source/apps/agent/node_modules/@earendil-works/pi-coding-agent/dist/core/slash-commands.js');
  assert.deepEqual(BUILTIN_SLASH_COMMANDS.map(item => '/' + item.name).sort(), commands.filter(name => !['/arminsayshi', '/dementedelves', '/debug'].includes(name)));
});

test('uncertain input acknowledgement retains the original identity', async () => {
  const calls = [];
  const client = { async input(text, options) {
    calls.push(options.inputId);
    if (calls.length === 1) throw new Error('acknowledgement lost');
    return { input_id: options.inputId, text, status: 'queued' };
  } };
  const queue = new InputQueue(client);
  await assert.rejects(queue.accept('same text'));
  await queue.accept('same text');
  assert.equal(calls[0], calls[1]);
  assert.equal(queue.consume('same text').input_id, calls[0]);
  assert.throws(() => queue.consume('same text'));
});

test('unknown and external-export commands cannot reach native handlers', async () => {
  const calls = [];
  const client = { subscribe() { return () => {}; }, async input(text, options) { calls.push(['accept', text]); return { input_id: options.inputId, text, status: 'queued' }; }, async call(method, params) { calls.push([method, params]); return {}; } };
  const actions = createHostActions(client, {});
  for (const command of ['/share', '/bug', '/debug', '/brand-new-upstream-command', '/unknown']) {
    assert.equal((await actions.submit(command, {})).action, 'reject');
  }
  assert.equal(calls.some(([method]) => ['effect', 'snapshot'].includes(method)), false);
  actions.dispose();
  assert.equal(typeof KernelCat.prototype.createSession, 'function');
});
