import assert from 'node:assert/strict';
import test from 'node:test';
import { controlledModels } from '../source/apps/agent/src/loopx/model-control.ts';
import { serialRpc } from '../source/apps/agent/src/clients/serial-rpc.ts';

const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};
const cancelled = () => ({ outcome: { status: 'failed', data: { reason: 'cancelled' } } });
const settlement = permit => ({ permit, status: 'cancelled', text: '' });

test('late cancelled-child requests do not poison another admitted child or its original settlement', async () => {
  const original = deferred(), calls = [];
  const owner = controlledModels({ async call(method, params) {
    calls.push({ method, params });
    if (method === 'plugin_begin') return { permit: params.id, routes: ['primary'] };
    if (method === 'child_model') {
      assert.equal(params.permit, 'a', 'the already cancelled child must never reach model admission');
      return original.promise;
    }
    if (method === 'plugin_settle') return cancelled();
    if (method === 'parent_after_cancel') return { alive: true };
    throw new Error(`unexpected host call: ${method}`);
  } });
  const rpc = serialRpc(owner, { childLanes: true });
  await rpc.call('plugin_begin', { id: 'a' });
  await rpc.call('plugin_begin', { id: 'b' });
  const a = rpc.call('child_model_turn', { permit: 'a' });
  await new Promise(resolve => setImmediate(resolve));
  const aSettlement = rpc.call('plugin_settle', settlement('a'));
  await rpc.call('plugin_settle', settlement('b'));
  assert.equal((await rpc.call('child_model_turn', { permit: 'b' })).reason, 'cancelled');
  assert.equal((await rpc.call('child_tool', { permit: 'b', call: { id: 'late' } })).stop, true);
  assert.equal(calls.filter(row => row.method === 'plugin_settle' && row.params.permit === 'a').length, 0,
    'settlement must wait for the original admitted model reply');
  original.resolve({ text: 'Original admitted reply', calls: [], receipt: 'a/model/original' });
  assert.equal((await a).receipt, 'a/model/original');
  assert.deepEqual(await aSettlement, cancelled());
  assert.equal((await rpc.call('child_model_turn', { permit: 'a' })).reason, 'cancelled');
  assert.deepEqual(await rpc.call('parent_after_cancel'), { alive: true });
  assert.equal(calls.filter(row => row.method === 'child_model').length, 1);
  assert.equal(calls.filter(row => row.method === 'plugin_settle').length, 2);
  assert.equal(calls.filter(row => row.method === 'child_tool').length, 0);
});

test('closing children wait for the original acknowledgement and identical settlements are not redispatched', async () => {
  const acknowledgement = deferred();
  let settlements = 0, models = 0;
  const rpc = controlledModels({ async call(method) {
    if (method === 'plugin_begin') return { permit: 'child', routes: ['primary'] };
    if (method === 'plugin_settle') { settlements++; return acknowledgement.promise; }
    if (method === 'child_model') { models++; throw new Error('late model admission'); }
    throw new Error(method);
  } });
  await rpc.call('plugin_begin', {});
  const closing = rpc.call('plugin_settle', settlement('child'));
  let completed = false;
  const late = rpc.call('child_model_turn', { permit: 'child' }).then(result => { completed = true; return result; });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(completed, false, 'cancellation is unknown until its original acknowledgement');
  acknowledgement.resolve(cancelled());
  assert.deepEqual(await closing, cancelled());
  assert.equal((await late).reason, 'cancelled');
  assert.deepEqual(await rpc.call('plugin_settle', settlement('child')), cancelled());
  assert.equal(settlements, 1);
  assert.equal(models, 0);
  await assert.rejects(rpc.call('plugin_settle', { permit: 'child', status: 'completed', text: 'forged' }),
    /differs from its original request/);
});

test('lost settlement acknowledgements retain the original error and fence all later work without retry', async () => {
  const uncertain = new Error('original settlement acknowledgement lost');
  let settlements = 0, models = 0;
  const owner = controlledModels({ async call(method) {
    if (method === 'plugin_begin') return { permit: 'child', routes: ['primary'] };
    if (method === 'plugin_settle') { settlements++; throw uncertain; }
    if (method === 'child_model') models++;
    throw new Error(`unexpected host call: ${method}`);
  } });
  const rpc = serialRpc(owner, { childLanes: true });
  await rpc.call('plugin_begin', {});
  await assert.rejects(rpc.call('plugin_settle', settlement('child')), error => error === uncertain);
  await assert.rejects(owner.call('child_model_turn', { permit: 'child' }), error => error === uncertain);
  await assert.rejects(owner.call('plugin_settle', settlement('child')), error => error === uncertain);
  await assert.rejects(rpc.call('parent_after_cancel'), error => error === uncertain);
  assert.equal(settlements, 1);
  assert.equal(models, 0);
});

test('unknown outcomes and unowned permits cannot be reclassified as a known cancellation', async () => {
  const rpc = controlledModels({ async call(method) {
    if (method === 'plugin_begin') return { permit: 'child', routes: ['primary'] };
    if (method === 'plugin_settle') return { outcome: { status: 'unknown', data: { reason: 'lost effect' } } };
    throw new Error(`unexpected host call: ${method}`);
  } });
  await rpc.call('plugin_begin', {});
  await assert.rejects(rpc.call('plugin_settle', settlement('child')), /settlement is unresolved/);
  await assert.rejects(rpc.call('child_model_turn', { permit: 'child' }), /settlement is unresolved/);
  await assert.rejects(rpc.call('child_model_turn', { permit: 'unowned' }), /no live owner permit/);
});
