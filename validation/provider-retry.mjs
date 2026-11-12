// Automatic retry of transient provider failures and their presentation: the worker
// repeats the same route only when the host permits it (each attempt a new admitted
// receipt), shows a notice, and says plainly when every route failed.
import assert from 'node:assert/strict';
import test from 'node:test';
import { controlledModels } from '../source/apps/agent/src/loopx/model-control.ts';
import { failureLabel, providerFailureText, retryNoticeText } from '../source/apps/agent/src/harness/pi/product-ui.mjs';
import { DEFAULT_BASH_TIMEOUT_SECONDS, withDefaultTimeout } from '../source/apps/agent/src/harness/pi/workspace-tools.mjs';

function host(replies) {
  const calls = [];
  const rpc = { async call(method, params) {
    calls.push({ method, params });
    if (method === 'hello') return {};
    if (method === 'prepare_turn') return { routes: ['ds', 'ds-pro'] };
    if (method === 'model' || method === 'child_model') return replies.shift();
    if (method === 'plugin_begin') return { permit: 'c', routes: ['ds', 'ds-pro'] };
    throw new Error(`unexpected host call: ${method}`);
  } };
  return { rpc, calls };
}
const retry = (route, attempt) => ({ failed: true, receipt: `r${attempt}`, retry: { route, attempt, max_retries: 4, delay_ms: 5, category: 'server', http_status: 503 } });

test('a permitted retry repeats the same route, then the reply is used', async () => {
  const { rpc, calls } = host([retry('ds', 1), retry('ds', 2), { text: 'ok', calls: [] }]);
  const notices = [];
  const owner = controlledModels(rpc, undefined, notice => notices.push(notice));
  await owner.call('hello', {});
  assert.equal((await owner.call('model_turn', {})).text, 'ok');
  assert.deepEqual(calls.filter(row => row.method === 'model').map(row => row.params.route), ['ds', 'ds', 'ds']);
  assert.deepEqual(notices.map(item => item.attempt), [1, 2]);
});

test('without a retry permit the next route is tried; a permit for another route is not used', async () => {
  let session = host([{ failed: true, receipt: 'r1' }, { text: 'pro', calls: [] }]);
  let owner = controlledModels(session.rpc);
  await owner.call('hello', {});
  assert.equal((await owner.call('model_turn', {})).text, 'pro');
  assert.deepEqual(session.calls.filter(row => row.method === 'model').map(row => row.params.route), ['ds', 'ds-pro']);
  // A retry naming a route other than the one just tried does not repeat anything.
  session = host([{ failed: true, receipt: 'r1' }, retry('ds', 1)]);
  owner = controlledModels(session.rpc);
  await owner.call('hello', {});
  await assert.rejects(owner.call('model_turn', {}), /routes exhausted/);
  assert.deepEqual(session.calls.filter(row => row.method === 'model').map(row => row.params.route), ['ds', 'ds-pro']);
});

test('children retry the same route under the same rule', async () => {
  const { rpc, calls } = host([retry('ds', 1), { text: 'child ok', calls: [], receipt: 'c/model' }]);
  const notices = [];
  const owner = controlledModels(rpc, undefined, notice => notices.push(notice));
  await owner.call('plugin_begin', { id: 'c' });
  assert.equal((await owner.call('child_model_turn', { permit: 'c' })).text, 'child ok');
  assert.deepEqual(calls.filter(row => row.method === 'child_model').map(row => row.params.route), ['ds', 'ds']);
  assert.equal(notices[0].child, true);
});

test('notices and the final failure say which route failed and how', () => {
  assert.equal(failureLabel({ route: 'ds', http_status: 503, category: 'server' }), 'ds HTTP 503 服务端错误');
  assert.equal(failureLabel({ route: 'ds', category: 'timeout' }), 'ds 超时');
  assert.equal(retryNoticeText({ route: 'ds', attempt: 2, max_retries: 4, delay_ms: 2300, category: 'rate_limit', http_status: 429 }),
    '模型服务暂时不可用（ds HTTP 429 限流），2.3s 后重试（第 2/4 次）');
  assert.match(retryNoticeText({ route: 'ds', attempt: 1, max_retries: 2, delay_ms: 500, category: 'server', child: true }), /^子代理的模型服务/);
  assert.equal(providerFailureText([
    { route: 'ds', http_status: 503, category: 'server' }, { route: 'ds', http_status: 503, category: 'server' },
    { route: 'ds-pro', category: 'timeout' }]),
  '模型调用失败：ds HTTP 503 服务端错误；ds-pro 超时（已自动重试 1 次）。本次请求没有完成，可以直接重新发送。');
  assert.match(providerFailureText(undefined), /所有模型路由都失败/);
});

test('bash gets a default timeout only when the model gives none', async () => {
  const seen = [];
  const tool = withDefaultTimeout({ name: 'bash', description: 'Run.', execute: async (_id, params) => { seen.push(params); return {}; } });
  await tool.execute('a', { command: 'true' });
  await tool.execute('b', { command: 'true', timeout: 5 });
  assert.deepEqual(seen, [{ command: 'true', timeout: DEFAULT_BASH_TIMEOUT_SECONDS }, { command: 'true', timeout: 5 }]);
  assert.match(tool.description, /Without timeout, a command stops after 600 s\./);
});
