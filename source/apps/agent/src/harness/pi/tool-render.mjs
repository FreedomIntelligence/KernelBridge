/**
 * Product presentation for tool calls: one status header plus a one-line
 * summary, with the original output behind Ctrl+O. Rendering never changes a
 * tool's semantics, its receipt, or the content returned to the model.
 */
import { Markdown, truncateToWidth, wrapTextWithAnsi } from '@earendil-works/pi-tui';
import { getMarkdownTheme, renderDiff } from '@earendil-works/pi-coding-agent';
import { isAbsolute, relative } from 'node:path';

const ANSI = /\x1b\[[0-9;?]*[ -\/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)/g;
export const clean = value => String(value ?? '').replace(ANSI, '').replace(/\r/g, '').replace(/\t/g, '  ')
  .replace(/[\x00-\x08\x0b-\x1f\x7f-\x9f]/g, ' ');
const oneLine = value => clean(value).replace(/\s+/g, ' ').trim();
const textOf = result => (result?.content ?? []).filter(block => block.type === 'text').map(block => block.text ?? '').join('\n');
const splitLines = text => {
  const rows = clean(text).replace(/\n+$/, '').split('\n');
  return rows.length === 1 && !rows[0] ? [] : rows;
};
const PREVIEW = 5;

export function formatDuration(ms) {
  if (ms == null || !Number.isFinite(ms)) return '';
  const seconds = ms / 1000;
  if (seconds < 10) return `${seconds.toFixed(1)}s`;
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes}m${String(Math.round(seconds % 60)).padStart(2, '0')}s`;
}

export function formatTokens(count) {
  if (!count) return '';
  return count >= 1000 ? `${(count / 1000).toFixed(1)}k` : String(count);
}

export function displayPath(path, cwd) {
  const value = oneLine(path);
  if (!value) return '';
  if (cwd && isAbsolute(value)) {
    const local = relative(cwd, value);
    if (!local.startsWith('..') && !isAbsolute(local)) return local || '.';
  }
  return value;
}

const scrub = (text, cwd) => cwd ? clean(text).split(cwd + '/').join('').split(cwd).join('.') : clean(text);

/** A width-aware component; rows are rebuilt on every frame so timers stay live. */
class Rows {
  constructor(build, pad = 0) { this.build = build; this.pad = Math.max(0, pad ?? 0); }
  render(width) {
    const inner = Math.max(8, width - this.pad);
    return this.build(inner).map(line => ' '.repeat(this.pad) + truncateToWidth(line, inner, ''));
  }
  invalidate() {}
}

function hanging(rows, width, theme) {
  const output = [];
  const inner = Math.max(4, width - 5);
  for (const row of rows) {
    for (const piece of wrapTextWithAnsi(row, inner)) {
      output.push((output.length ? '     ' : theme.fg('dim', '  ⎿  ')) + piece);
    }
  }
  return output;
}

function dot(theme, context) {
  if (context.isPartial) return theme.fg(Math.floor(Date.now() / 600) % 2 ? 'muted' : 'dim', '⏺');
  return theme.fg(context.isError ? 'error' : 'success', '⏺');
}

function headerRows(theme, context, title, detail, width) {
  const text = `${dot(theme, context)} ${theme.fg('toolTitle', theme.bold(title))}`
    + (detail ? `${theme.fg('muted', '(')}${detail}${theme.fg('muted', ')')}` : '');
  const rows = wrapTextWithAnsi(text, width);
  return rows.length > 3 ? [...rows.slice(0, 2), truncateToWidth(rows[2], width - 1, '') + '…'] : rows;
}

const expandKey = context => context.keyText?.('app.tools.expand') ?? 'Ctrl+O';
const more = (count, context, theme) => theme.fg('muted', `… 另有 ${count} 行（${expandKey(context)} 展开）`);

function preview(rows, context, theme, { tail = false } = {}) {
  if (context.expanded || rows.length <= PREVIEW) return rows.map(row => theme.fg('toolOutput', row));
  const shown = tail ? rows.slice(-PREVIEW) : rows.slice(0, PREVIEW);
  const hint = more(rows.length - PREVIEW, context, theme);
  return tail ? [hint, ...shown.map(row => theme.fg('toolOutput', row))] : [...shown.map(row => theme.fg('toolOutput', row)), hint];
}

function failure(result, context, theme) {
  const rows = splitLines(scrub(textOf(result), context.cwd).replace(/^\s*Error:\s*/, ''));
  const shown = context.expanded ? rows : rows.slice(0, 3);
  return [theme.fg('error', `失败：${shown[0] ?? '工具未返回错误详情'}`), ...shown.slice(1).map(row => theme.fg('error', row)),
    ...(!context.expanded && rows.length > 3 ? [more(rows.length - 3, context, theme)] : [])];
}

const notes = rows => rows.filter(row => !/^\[.*\]$/.test(row.trim()) && row.trim());
const TEST_COMMAND = /\b(test|tests|pytest|unittest|jest|vitest|mocha|tsc|build|make|cmake|ctest|cargo|go\s+(test|build|vet)|gradle|mvn|ruff|mypy|eslint|lint|check)\b/i;

const NATIVE = {
  read: {
    title: 'Read',
    detail: (args, cwd) => displayPath(args?.path ?? args?.file_path, cwd) + (args?.offset ? ` · 第 ${args.offset} 行起` : ''),
    result(result, context, theme) {
      const rows = splitLines(textOf(result)).filter(row => !/^\[\d+ more lines in file\./.test(row));
      const truncation = result.details?.truncation;
      const summary = rows.length ? `读取 ${rows.length} 行${truncation?.truncated ? ` · 文件共 ${truncation.totalLines} 行` : ''}`
        : (result.content ?? []).some(block => block.type === 'image') ? '读取图片' : '文件为空';
      return context.expanded ? [theme.fg('muted', summary), ...rows.map(row => theme.fg('toolOutput', row))] : [theme.fg('muted', summary)];
    },
  },
  write: {
    title: 'Write',
    detail: (args, cwd) => displayPath(args?.path ?? args?.file_path, cwd),
    result(_result, context, theme) {
      const rows = splitLines(context.args?.content ?? '');
      return [theme.fg('muted', `已写入 ${rows.length} 行`), ...(context.expanded ? rows.map(row => theme.fg('toolOutput', row)) : [])];
    },
  },
  edit: {
    title: 'Update',
    detail: (args, cwd) => displayPath(args?.path ?? args?.file_path, cwd),
    result(result, context, theme) {
      const diff = typeof result.details?.diff === 'string' ? result.details.diff : '';
      const rows = diff.split('\n');
      const added = rows.filter(row => /^\+\s*\d/.test(row)).length;
      const removed = rows.filter(row => /^-\s*\d/.test(row)).length;
      const summary = theme.fg('muted', `已更新 ${displayPath(context.args?.path ?? context.args?.file_path, context.cwd)}：`)
        + theme.fg('success', `+${added}`) + ' ' + theme.fg('error', `−${removed}`);
      if (!diff) return [summary];
      const rendered = renderDiff(diff, { filePath: context.args?.path }).split('\n');
      const limit = 12;
      return [summary, ...(context.expanded || rendered.length <= limit ? rendered
        : [...rendered.slice(0, limit), more(rendered.length - limit, context, theme)])];
    },
  },
  bash: {
    title: 'Bash',
    detail: args => oneLine(args?.command),
    result(result, context, theme) {
      let rows = splitLines(textOf(result));
      let code = context.isError ? null : 0;
      const status = rows.length ? /^Command exited with code (\d+)$/.exec(rows.at(-1).trim()) : null;
      if (status) { code = Number(status[1]); rows = rows.slice(0, -1); }
      while (rows.length && !rows.at(-1).trim()) rows.pop();
      if (context.isPartial) {
        const elapsed = context.state.startedAt ? formatDuration(Date.now() - context.state.startedAt) : '';
        return [...rows.slice(-PREVIEW).map(row => theme.fg('toolOutput', row)),
          theme.fg('muted', `运行中${elapsed ? ' · ' + elapsed : ''}${rows.length > PREVIEW ? ` · 共 ${rows.length} 行` : ''}`)];
      }
      const tail = context.isError || TEST_COMMAND.test(context.args?.command ?? '');
      const meta = [formatDuration(context.durationMs), code == null ? '失败' : `退出码 ${code}`].filter(Boolean).join(' · ');
      const body = rows.length ? preview(rows, context, theme, { tail }) : [theme.fg('muted', '（无输出）')];
      return [...body, theme.fg(code === 0 ? 'muted' : 'error', meta)];
    },
  },
  grep: {
    title: 'Search',
    detail: (args, cwd) => [args?.pattern ? `"${oneLine(args.pattern)}"` : '', args?.path ? displayPath(args.path, cwd) : '',
      args?.glob ? oneLine(args.glob) : ''].filter(Boolean).join(' · '),
    result(result, context, theme) {
      const rows = notes(splitLines(textOf(result)));
      if (!rows.length || rows[0] === 'No matches found') return [theme.fg('muted', '未找到匹配')];
      return [theme.fg('muted', `找到 ${rows.length} 处匹配`), ...(context.expanded ? rows.map(row => theme.fg('toolOutput', row)) : [])];
    },
  },
  find: {
    title: 'Find',
    detail: (args, cwd) => [args?.pattern ? oneLine(args.pattern) : '', args?.path ? displayPath(args.path, cwd) : ''].filter(Boolean).join(' · '),
    result(result, context, theme) {
      const rows = notes(splitLines(textOf(result)));
      if (!rows.length || rows[0].startsWith('No files found')) return [theme.fg('muted', '未找到文件')];
      return [theme.fg('muted', `找到 ${rows.length} 个文件`), ...(context.expanded ? rows.map(row => theme.fg('toolOutput', row)) : [])];
    },
  },
  ls: {
    title: 'List',
    detail: (args, cwd) => displayPath(args?.path ?? '.', cwd),
    result(result, context, theme) {
      const rows = notes(splitLines(textOf(result)));
      if (!rows.length || rows[0] === '(empty directory)') return [theme.fg('muted', '空目录')];
      return [theme.fg('muted', `${rows.length} 项`), ...(context.expanded ? rows.map(row => theme.fg('toolOutput', row)) : [])];
    },
  },
};

// ---------------------------------------------------------------- exploration groups
// Consecutive read/search/list calls collapse into one row, as in other coding
// CLIs. Assistant text, any other tool and user input end a group. Ctrl+O shows
// every call individually.
const EXPLORE = new Set(['read', 'grep', 'find', 'ls']);
const groupOf = new Map();
let currentGroup = null;

export function explorationBoundary() { currentGroup = null; }

function joinGroup(id, name) {
  if (groupOf.has(id)) return groupOf.get(id);
  // Only the first registration of a call affects grouping; re-renders do not.
  if (!EXPLORE.has(name)) { groupOf.set(id, null); currentGroup = null; return null; }
  currentGroup ??= { members: [], leaderView: undefined };
  currentGroup.members.push({ id, name, status: 'running' });
  groupOf.set(id, currentGroup);
  currentGroup.leaderView?.();
  return currentGroup;
}

/** Rebuild groups for restored history: blocks in transcript order. */
export function seedExplorationGroups(blocks) {
  explorationBoundary();
  for (const block of blocks) {
    if (block.type === 'tool') {
      const group = joinGroup(block.id, block.name);
      const member = group?.members.find(item => item.id === block.id);
      if (member && block.status) member.status = block.status;
    } else explorationBoundary();
  }
  explorationBoundary();
}

function groupSummary(group) {
  const count = name => group.members.filter(member => member.name === name).length;
  const parts = [count('read') && `读取 ${count('read')} 个文件`, count('grep') && `搜索 ${count('grep')} 次`,
    count('find') && `查找 ${count('find')} 次`, count('ls') && `列出 ${count('ls')} 个目录`].filter(Boolean);
  const failed = group.members.filter(member => member.status === 'failed').length;
  return { text: parts.join(' · '), failed, running: group.members.some(member => member.status === 'running') };
}

function groupRows(group, theme, context, width) {
  const { text, failed, running } = groupSummary(group);
  const dot = running ? theme.fg(Math.floor(Date.now() / 600) % 2 ? 'muted' : 'dim', '⏺') : theme.fg(failed ? 'error' : 'success', '⏺');
  const rows = wrapTextWithAnsi(`${dot} ${theme.fg('toolTitle', theme.bold('浏览'))} ${theme.fg('muted', text)}`, width);
  return [...rows, ...hanging([(failed ? theme.fg('error', `${failed} 次失败 · `) : '') + theme.fg('muted', `${expandKey(context)} 展开`)], width, theme)];
}

/** Attach product renderers to a native Pi tool definition (project or isolated workspace). */
export function withProductRenderers(tool) {
  const spec = NATIVE[tool.name];
  if (!spec) return tool;
  return {
    ...tool,
    renderShell: 'self',
    renderCall(args, theme, context) {
      if (context.executionStarted && !context.state.startedAt) context.state.startedAt = Date.now();
      const group = joinGroup(context.toolCallId, tool.name);
      if (group?.members[0].id === context.toolCallId) group.leaderView = context.invalidate;
      return new Rows(width => {
        if (group && group.members.length > 1 && !context.expanded) {
          return group.members[0].id === context.toolCallId ? groupRows(group, theme, context, width) : [];
        }
        return headerRows(theme, context, spec.title, spec.detail(args, context.cwd), width);
      }, context.outputPad);
    },
    renderResult(result, options, theme, context) {
      const view = { ...context, expanded: options.expanded, isPartial: options.isPartial };
      const group = groupOf.get(context.toolCallId);
      const member = group?.members.find(item => item.id === context.toolCallId);
      if (member) {
        const status = options.isPartial ? 'running' : context.isError ? 'failed' : 'completed';
        if (member.status !== status) { member.status = status; if (group.members[0].id !== context.toolCallId) group.leaderView?.(); }
      }
      return new Rows(width => {
        if (group && group.members.length > 1 && !options.expanded) return [];
        let rows;
        try {
          rows = !view.isPartial && view.isError ? failure(result, view, theme) : spec.result(result, view, theme);
        } catch { rows = splitLines(textOf(result)).slice(0, PREVIEW); }
        return hanging(rows, width, theme);
      }, context.outputPad);
    },
  };
}

// ---------------------------------------------------------------- receipt and delegate tools

const subagentRows = new Map();
const subagentViews = new Map();

/** Host state is the authority for child progress; Pi only renders it. */
export function updateSubagentProgress(children = []) {
  for (const child of children) {
    if (!child.tool_call_id) continue;
    subagentRows.set(child.tool_call_id, child);
    subagentViews.get(child.tool_call_id)?.();
  }
}

const REASONS = { stopped: '已停止', worker_exited: '会话退出时中断', count_limit: '超出数量上限', concurrency_limit: '超出并发上限', context_limit: '上下文已满', step_limit: '模型调用上限', tool_limit: '读取上限', timeout: '超时', cancelled: '已取消',
  budget_exhausted: '预算耗尽', providers_failed: '模型调用失败', provider_unconfigured: '模型未配置',
  output_limit: '输出过长', unexpected_tool: '分析模式调用了工具', plugin_failed: '子会话失败' };
export const reasonLabel = reason => REASONS[reason] ?? reason ?? '';
const TOOL_LABELS = { run_command: '运行', write_file: '写入', edit_file: '编辑', read_file: '读取', list_files: '列出文件', search_text: '搜索', load_skill: '读取技能',
  read_receipt: '读取回执', knowledge_search: '检索知识库', knowledge_show: '查看知识', knowledge_read: '读取知识',
  knowledge_source: '查看来源', read_learned_skill: '读取技能' };

export function delegateTitle(args) {
  const description = oneLine(args?.description);
  if (description) return description;
  return truncateToWidth(oneLine(args?.task), 36, '…');
}

export function childMetrics(child, durationMs) {
  return [child.tool_calls != null ? `工具 ${child.tool_calls} 次` : '', child.steps != null ? `模型 ${child.steps}` : '',
    child.tokens ? `${formatTokens(child.tokens)} tokens` : '', formatDuration(durationMs)].filter(Boolean).join(' · ');
}

function activityLine(child, elapsed) {
  const activity = child?.latest_tool ? `${TOOL_LABELS[child.latest_tool] ?? child.latest_tool}${child.latest_detail ? ' ' + oneLine(child.latest_detail) : ''}`
    : child?.steps ? '思考中' : '启动中';
  return [activity, childMetrics(child ?? {}, elapsed)].filter(Boolean).join(' · ');
}

function delegateCall(args, theme, context) {
  if (context.executionStarted && !context.state.startedAt) context.state.startedAt = Date.now();
  subagentViews.set(context.toolCallId, context.invalidate);
  return new Rows(width => {
    const rows = headerRows(theme, context, oneLine(args?.agent) || '子代理', delegateTitle(args), width);
    if (!context.isPartial) return rows;
    const elapsed = context.state.startedAt ? Date.now() - context.state.startedAt : undefined;
    return [...rows, ...hanging([theme.fg('muted', activityLine(subagentRows.get(context.toolCallId), elapsed))], width, theme)];
  }, context.outputPad);
}

function settledRows(child, durationMs, expanded, theme, context, width) {
  const failed = child.status === 'failed' || child.status === 'unknown';
  const metrics = childMetrics(child, durationMs);
  const head = failed ? theme.fg('error', `失败：${reasonLabel(child.reason) || child.error || '子代理未完成'}`)
    : child.partial ? theme.fg('warning', `部分结果（${reasonLabel(child.completion_reason ?? child.reason)}）`) : theme.fg('success', '完成');
  const rows = [head + (metrics ? theme.fg('muted', ' · ' + metrics) : '')];
  if (!expanded) {
    if (child.summary) rows.push(theme.fg('muted', `${expandKey(context)} 查看报告`));
    return hanging(rows, width, theme);
  }
  const output = hanging(rows, width, theme);
  if (child.summary) {
    const markdown = new Markdown(clean(child.summary), 0, 0, getMarkdownTheme());
    output.push(...markdown.render(Math.max(10, width - 5)).map(line => '     ' + line));
  }
  output.push('     ' + theme.fg('dim', `${child.mode ?? ''} · 建议性结论，不是验收证据`));
  return output;
}

function delegateResult(result, options, theme, context) {
  const outcome = result.details?.outcome ?? {};
  const data = outcome.data ?? {};
  return new Rows(width => {
    if (result.details?.stopped) return hanging([theme.fg('warning', '已停止')], width, theme);
    const live = subagentRows.get(context.toolCallId);
    if (data.background) {
      const child = { ...data, ...live };
      if (!live || live.status === 'running') {
        const elapsed = context.state.startedAt ? Date.now() - context.state.startedAt : undefined;
        return hanging([theme.fg('accent', '后台运行') + theme.fg('muted', ' · ' + activityLine(live, elapsed))], width, theme);
      }
      context.state.finishedAt ??= Date.now();
      const duration = context.state.startedAt ? context.state.finishedAt - context.state.startedAt : undefined;
      return settledRows(child, duration, options.expanded, theme, context, width);
    }
    const child = { ...live, ...data, status: result.isError || outcome.status !== 'completed' ? 'failed' : 'completed' };
    return settledRows(child, context.durationMs, options.expanded, theme, context, width);
  }, context.outputPad);
}

function receiptCall(name) {
  return (args, theme, context) => new Rows(width => {
    const entries = Object.entries(args ?? {}).filter(([, value]) => value != null && value !== '');
    const scalars = entries.filter(([, value]) => typeof value === 'string' || typeof value === 'number').map(([, value]) => oneLine(value));
    // A structured-only call still names what it was given, so no row reads as a bare tool name.
    const detail = (scalars.length ? scalars : entries.map(([key]) => key)).join(' · ');
    return headerRows(theme, context, name, detail.length > 80 ? detail.slice(0, 79) + '…' : detail, width);
  }, context.outputPad);
}

function prettyRows(value, prefix = '') {
  if (value == null || typeof value !== 'object') return [`${prefix}${clean(value).slice(0, 400)}`];
  const rows = [];
  for (const [key, item] of Object.entries(value)) {
    if (item != null && typeof item === 'object' && Object.keys(item).length && rows.length < 200) {
      rows.push(`${prefix}${key}:`);
      rows.push(...prettyRows(item, prefix + '  '));
    } else rows.push(`${prefix}${key}: ${clean(typeof item === 'string' ? item : JSON.stringify(item)).slice(0, 400)}`);
  }
  return rows.slice(0, 400);
}

export function renderReceiptResult(result, options, theme, context = {}) {
  const outcome = result.details?.outcome;
  const data = outcome?.data ?? {};
  return new Rows(width => {
    if (result.details?.stopped) return hanging([theme.fg('warning', '已停止')], width, theme);
    const failed = result.isError || (outcome && outcome.status !== 'completed');
    const status = failed ? theme.fg('error', `失败${data.reason || data.error ? '：' + oneLine(data.reason ?? data.error).slice(0, 160) : ''}`)
      : outcome?.status === 'completed' ? theme.fg('success', '已完成') : theme.fg('muted', '处理中');
    const facts = [data.correct === true ? '正确性通过' : null, data.fixture_only ? '本地测试' : null,
      !failed && data.reason ? oneLine(data.reason).slice(0, 160) : null].filter(Boolean).join(' · ');
    const rows = [status + (facts ? theme.fg('muted', ' · ' + facts) : '')];
    if (options.expanded) rows.push(...prettyRows(outcome ?? result.details ?? {}).map(row => theme.fg('toolOutput', row)));
    return hanging(rows, width, theme);
  }, context.outputPad);
}

/** Renderers for Host-sealed direct tools; delegate gets live child progress. */
export function directRenderers(name) {
  const boundary = render => (args, theme, context) => { joinGroup(context.toolCallId, name); return render(args, theme, context); };
  if (name === 'delegate') return { renderShell: 'self', renderCall: boundary(delegateCall), renderResult: delegateResult };
  return { renderShell: 'self', renderCall: boundary(receiptCall(name)), renderResult: renderReceiptResult };
}
