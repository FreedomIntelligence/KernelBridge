import test from 'node:test';
import assert from 'node:assert/strict';
import { Duplex } from 'node:stream';
import { setImmediate as immediate } from 'node:timers/promises';
import { ApplicationClient, acknowledgementTimeout } from '../source/apps/agent/src/application/client.mjs';

class Socket extends Duplex {
  writes = [];
  _read() {}
  _write(bytes, _encoding, done) { this.writes.push(JSON.parse(bytes.toString())); done(); }
  reply(id, result) { this.push(Buffer.from(JSON.stringify({ id, result }) + '\n')); }
}

test('only durable snapshot and restore operations get a longer acknowledgement deadline', () => {
  for (const operation of ['checkpoint', 'fork', 'reset', 'clone']) {
    assert.equal(acknowledgementTimeout('snapshot', { operation }), 300000);
  }
  assert.equal(acknowledgementTimeout('select_session', { path: '/retained/session.jsonl' }), 300000);
  for (const method of ['status', 'accept', 'control', 'subscribe', 'select_session']) {
    assert.equal(acknowledgementTimeout(method), 30000);
  }
});

test('snapshot waits past 30 seconds for its original acknowledgement without redispatch', async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const socket = new Socket(), client = new ApplicationClient(socket);
  try {
    let outcome = 'pending';
    const result = client.call('snapshot', { operation: 'checkpoint', pi_session_id: 'session', entry_id: 'leaf' });
    result.then(() => { outcome = 'completed'; }, () => { outcome = 'failed'; });
    t.mock.timers.tick(45000); await immediate();
    assert.equal(outcome, 'pending');
    assert.equal(socket.writes.length, 1);
    socket.reply(socket.writes[0].id, { entry_id: 'leaf', retained: true });
    assert.deepEqual(await result, { entry_id: 'leaf', retained: true });
    assert.equal(socket.writes.length, 1);
  } finally { client.close(); }
});

test('an expired acknowledgement keeps its original identity and never repeats the operation', async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const socket = new Socket(), client = new ApplicationClient(socket);
  try {
    const result = client.call('snapshot', { operation: 'fork', pi_session_id: 'session', entry_id: 'leaf' }, 25);
    const rejected = assert.rejects(result, error => {
      assert.equal(error.code, 'ACKNOWLEDGEMENT_TIMEOUT');
      assert.equal(error.requestId, socket.writes[0].id);
      assert.deepEqual(error.original, { pi_session_id: 'session', entry_id: 'leaf', operation: 'fork' });
      return /outcome is unknown/.test(error.message);
    });
    t.mock.timers.tick(26); await rejected;
    socket.reply(socket.writes[0].id, { retained: true }); await immediate();
    assert.equal(socket.writes.length, 1);
    assert.equal(client.pending.size, 0);
    const status = client.state();
    socket.reply(socket.writes[1].id, { checkpoint: 'original' });
    assert.deepEqual(await status, { checkpoint: 'original' });
    assert.deepEqual(socket.writes.map(row => row.method), ['snapshot', 'status']);
  } finally { client.close(); }
});
