// /model and /effort presentation: the picker (Claude Code's ←→ effort in /model, Codex's
// model + effort choice), level arguments, the summary and the footer segment.
import test from 'node:test';
import assert from 'node:assert/strict';
import { visibleWidth } from '../../source/apps/agent/node_modules/@earendil-works/pi-tui/dist/index.js';
import { levelLabel, modelChanged, modelPicker, modelSummary, orderedLevels, parseLevel } from '../../source/apps/agent/src/harness/pi/model-picker.mjs';

const LEVELS = ['off', 'low', 'high', 'max'];
const info = () => ({ current: 'ds', allowed_routes: ['ds', 'ds-pro'], routes: [
  { route: 'ds', model: 'deepseek-flash', levels: LEVELS, level: 'off', configured: 'off', current: true },
  { route: 'ds-pro', model: 'deepseek-v4-pro', levels: LEVELS, level: 'high', configured: 'off', current: false },
  { route: 'local', model: 'qwen-coder', levels: [], level: null, configured: null, current: false },
] });
const KEYS = { up: '\x1b[A', down: '\x1b[B', right: '\x1b[C', left: '\x1b[D', enter: '\r', escape: '\x1b' };

function open(options) {
  let result = 'pending';
  const picker = modelPicker(info(), value => { result = value; }, () => {}, options);
  const press = (...keys) => keys.forEach(key => picker.handleInput(KEYS[key] ?? key));
  return { picker, press, result: () => result };
}

test('levels are ordered and labelled; null is the provider default', () => {
  assert.deepEqual(orderedLevels(['max', 'off', 'high', 'low']), LEVELS);
  assert.deepEqual(LEVELS.map(levelLabel), ['关', '低', '高', '最大']);
  assert.equal(levelLabel(null), '默认');
});

test('the picker opens on the current route, shows each route level and the choice row', () => {
  const { picker } = open();
  const lines = picker.render(80);
  assert.equal(lines[0], '选择模型和推理档位');
  assert.match(lines[2], /^❯ ✔ deepseek-flash\s+ds\s+思考 ‹ 关 ›$/);
  assert.match(lines[3], /^ {4}deepseek-v4-pro\s+ds-pro\s+思考 {3}高$/);
  assert.match(lines[4], /^ {4}qwen-coder\s+local\s*$/, 'a route without levels shows none');
  assert.ok(lines.some(line => line.includes('[关]  低  高  最大') && line.includes('配置默认：关')));
  assert.equal(lines.at(-1), '↑↓ 选择模型 · ←→ 推理档位 · Enter 确认 · Esc 取消');
});

test('←→ change the highlighted route level; Enter returns route and level', () => {
  let session = open();
  session.press('right', 'right', 'enter');
  assert.deepEqual(session.result(), { level: 'high' }, 'same route: only the level');
  session = open();
  session.press('down', 'right', 'enter');
  assert.deepEqual(session.result(), { route: 'ds-pro', level: 'max' });
  session = open();
  session.press('down', 'enter');
  assert.deepEqual(session.result(), { route: 'ds-pro' }, 'unchanged level is not sent');
  session = open();
  session.press('left', 'left', 'enter');
  assert.deepEqual(session.result(), {}, 'already at the lowest level: nothing changes');
  session = open();
  session.press('right', 'escape');
  assert.equal(session.result(), null);
});

test('/effort opens the same picker locked to the current route', () => {
  const session = open({ focusLevel: true });
  session.press('down', 'right', 'enter');
  assert.deepEqual(session.result(), { level: 'low' });
  assert.equal(session.picker.render(80)[0], '推理档位');
  assert.equal(session.picker.render(80).at(-1), '←→ 推理档位 · Enter 确认 · Esc 取消');
});

test('every picker row fits narrow terminals', () => {
  for (const width of [32, 40, 48]) {
    assert.ok(open().picker.render(width).every(line => visibleWidth(line) <= width), String(width));
  }
});

test('level arguments accept names, Chinese labels and auto; others are explained', () => {
  const route = info().routes[0];
  assert.deepEqual(parseLevel('high', route), { value: 'high' });
  assert.deepEqual(parseLevel('最大', route), { value: 'max' });
  assert.deepEqual(parseLevel('关闭', route), { value: 'off' });
  assert.deepEqual(parseLevel('AUTO', route), { value: 'auto' });
  assert.deepEqual(parseLevel('默认', route), { value: 'auto' });
  assert.match(parseLevel('xhigh', route).error, /不支持“xhigh”。可选：off（关）、low（低）、high（高）、max（最大），或 auto/);
  assert.match(parseLevel('high', info().routes[2]).error, /qwen-coder 没有可选的推理档位/);
});

test('summary and confirmation name the model, route and level', () => {
  assert.equal(modelSummary(info()), [
    '当前模型：deepseek-flash（ds） · 思考 关',
    '推理档位：关 / 低 / 高 / 最大（配置默认 关）',
    '其他模型：deepseek-v4-pro（ds-pro） · 思考 高、qwen-coder（local）',
    '/model 选择模型和档位 · /effort 档位 · shift+tab 切换档位'].join('\n'));
  const changed = info();
  changed.routes[0].current = false; changed.routes[1].current = true;
  assert.equal(modelChanged(changed), '已切换：deepseek-v4-pro（ds-pro） · 思考 高。下一次模型调用生效。');
});
