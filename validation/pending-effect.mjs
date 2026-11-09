import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { SCHEMA } from '../source/apps/agent/src/generated/runtime.ts';
const require = createRequire(new URL('../source/apps/agent/package.json', import.meta.url));
const Ajv = require('ajv/dist/2020');

test('pending actions preserve long original identities for recovery', () => {
  const validate = new Ajv().compile(SCHEMA.$defs.RuntimeState.properties.pending_effect_ids);
  const identity = `runtime-task/${'a'.repeat(64)}/step/17/model/primary/call/0/adapter/confirm/${'b'.repeat(64)}`;
  assert.ok(identity.length > 128);
  assert.equal(validate([identity]), true);
  assert.equal(validate(['']), false);
  assert.equal(validate([123]), false);
});
