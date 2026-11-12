import { matchesKey, truncateToWidth, visibleWidth } from '@earendil-works/pi-tui';

/** Display names for reasoning levels; "off" disables thinking where it is a toggle. */
export const LEVEL_LABELS = Object.freeze({ off: '关', minimal: '极低', low: '低', medium: '中', high: '高', xhigh: '超高', max: '最大' });
const ORDER = ['off', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'];

export function levelLabel(level) {
  return level == null ? '默认' : LEVEL_LABELS[level] ?? level;
}

/** Levels in ascending order, as the host offers them for one route. */
export function orderedLevels(levels) {
  return ORDER.filter(level => (levels ?? []).includes(level));
}

const pad = (text, width) => text + ' '.repeat(Math.max(0, width - visibleWidth(text)));

/**
 * Model and reasoning picker in the style of Claude Code's /model and Codex's
 * model/effort selection: ↑↓ choose a route, ←→ choose its reasoning level,
 * Enter applies both, Esc cancels. `info` is the host's configure(model) reply.
 * `focusLevel` opens it for /effort: the current route, adjusting the level.
 */
export function modelPicker(info, finish, requestRender, { focusLevel = false } = {}) {
  const routes = info.routes ?? [];
  let selected = Math.max(0, routes.findIndex(item => item.current));
  // Each row keeps its own pending level, starting from the route's current one.
  const pending = routes.map(item => {
    const levels = orderedLevels(item.levels);
    return levels.includes(item.level) ? item.level : item.configured ?? null;
  });
  const step = delta => {
    const levels = orderedLevels(routes[selected]?.levels);
    if (!levels.length) return;
    const index = levels.indexOf(pending[selected]);
    const next = index < 0 ? (delta > 0 ? 0 : levels.length - 1) : Math.max(0, Math.min(levels.length - 1, index + delta));
    pending[selected] = levels[next];
  };
  return {
    focused: false,
    invalidate() {},
    render(width) {
      const clip = text => truncateToWidth(text, width, '…');
      const modelWidth = Math.min(28, Math.max(8, ...routes.map(item => visibleWidth(item.model))));
      const routeWidth = Math.min(12, Math.max(4, ...routes.map(item => visibleWidth(item.route))));
      const lines = [clip(focusLevel ? '推理档位' : '选择模型和推理档位'), ''];
      routes.forEach((item, index) => {
        const mark = index === selected ? '❯' : ' ';
        const check = item.current ? '✔' : ' ';
        const levels = orderedLevels(item.levels);
        const level = levels.length
          ? (index === selected ? `思考 ‹ ${levelLabel(pending[index])} ›` : `思考   ${levelLabel(pending[index])}`)
          : '';
        lines.push(clip(`${mark} ${check} ${pad(item.model, modelWidth)}  ${pad(item.route, routeWidth)}  ${level}`));
      });
      const current = routes[selected];
      const levels = orderedLevels(current?.levels);
      if (levels.length) {
        lines.push('', clip('  ' + levels.map(level => level === pending[selected] ? `[${levelLabel(level)}]` : levelLabel(level)).join('  ')
          + (current.configured != null ? `   配置默认：${levelLabel(current.configured)}` : '')));
      }
      lines.push('', clip(`${routes.length > 1 && !focusLevel ? '↑↓ 选择模型 · ' : ''}${levels.length ? '←→ 推理档位 · ' : ''}Enter 确认 · Esc 取消`));
      return lines;
    },
    handleInput(data) {
      if (matchesKey(data, 'escape') || matchesKey(data, 'ctrl+c')) return finish(null);
      if (matchesKey(data, 'enter')) {
        const item = routes[selected];
        if (!item) return finish(null);
        const levels = orderedLevels(item.levels);
        const level = levels.length && pending[selected] !== item.level ? pending[selected] : undefined;
        return finish({ ...(item.current ? {} : { route: item.route }), ...(level !== undefined && level !== null ? { level } : {}) });
      }
      if (!focusLevel && matchesKey(data, 'up')) selected = Math.max(0, selected - 1);
      else if (!focusLevel && matchesKey(data, 'down')) selected = Math.min(routes.length - 1, selected + 1);
      else if (matchesKey(data, 'left')) step(-1);
      else if (matchesKey(data, 'right') || matchesKey(data, 'tab')) step(1);
      requestRender();
    },
  };
}

const routeLine = item => `${item.model}（${item.route}）${orderedLevels(item.levels).length ? ` · 思考 ${levelLabel(item.level)}` : ''}`;

/** /model without a picker (and /effort status): the current route first, then the others. */
export function modelSummary(info) {
  const routes = info.routes ?? [];
  const current = routes.find(item => item.current);
  const others = routes.filter(item => !item.current);
  return [`当前模型：${current ? routeLine(current) : info.current}`,
    ...(current && orderedLevels(current.levels).length
      ? [`推理档位：${orderedLevels(current.levels).map(levelLabel).join(' / ')}${current.configured != null ? `（配置默认 ${levelLabel(current.configured)}）` : ''}`] : []),
    ...(others.length ? [`其他模型：${others.map(routeLine).join('、')}`] : []),
    '/model 选择模型和档位 · /effort 档位 · shift+tab 切换档位'].join('\n');
}

/** Confirmation after a change; it applies from the next model call. */
export function modelChanged(result) {
  const current = (result.routes ?? []).find(item => item.current);
  return current ? `已切换：${routeLine(current)}。下一次模型调用生效。` : '已更新模型配置。';
}

const ALIASES = { auto: 'auto', 默认: 'auto', default: 'auto', none: 'off', disabled: 'off', 关闭: 'off' };

/** A level argument: English names, the Chinese labels, or auto (the configured default). */
export function parseLevel(text, route) {
  const raw = String(text ?? '').trim().toLowerCase();
  const level = ALIASES[raw] ?? Object.entries(LEVEL_LABELS).find(([, label]) => label === raw)?.[0] ?? raw;
  const levels = orderedLevels(route?.levels);
  if (level === 'auto') return levels.length ? { value: 'auto' } : { error: `${route?.model ?? '当前模型'} 没有可选的推理档位。` };
  if (!levels.includes(level)) {
    return { error: levels.length ? `${route.model} 不支持“${text}”。可选：${levels.map(item => `${item}（${levelLabel(item)}）`).join('、')}，或 auto。`
      : `${route?.model ?? '当前模型'} 没有可选的推理档位。` };
  }
  return { value: level };
}
