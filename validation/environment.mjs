import test from 'node:test';
import assert from 'node:assert/strict';
import { withProcessEnvironment } from '../source/apps/agent/src/harness/pi/environment.mjs';

test('concurrent SDK environment scopes serialize and nested scopes restore after errors', async () => {
  const name = 'KERNELBRIDGE_ENVIRONMENT_REGRESSION';
  const original = process.env[name];
  const observed = [];
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  const first = withProcessEnvironment({ [name]: 'first' }, async () => {
    observed.push(process.env[name]);
    await gate;
    await assert.rejects(withProcessEnvironment({ [name]: 'nested' }, async () => {
      observed.push(process.env[name]); throw new Error('nested failure');
    }), /nested failure/);
    observed.push(process.env[name]);
  });
  const second = withProcessEnvironment({ [name]: 'second' }, async () => { observed.push(process.env[name]); });
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(observed, ['first']);
  release();
  await Promise.all([first, second]);
  assert.deepEqual(observed, ['first', 'nested', 'first', 'second']);
  assert.equal(process.env[name], original);
});
