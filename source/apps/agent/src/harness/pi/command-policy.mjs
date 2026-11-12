import { visibleWidth } from '@earendil-works/pi-tui';

/** One catalogue drives both the command gate and native editor completion. */
export const COMMAND_POLICY_VERSION = 'kernelbridge.commands/v3';
export const PRODUCT_COMMANDS = Object.freeze([
  ['goal', '设置持续目标；不带参数查看目标', '[目标内容 | pause | resume | clear]'],
  ['help', '查看可用命令与快捷键'],
  ['latest', '回到最新消息'],
  ['subagents', '查看子代理、触发方式和执行结果'],
  ['agents', '查看可用子代理'],
  ['trace', '在浏览器中打开实时轨迹（Lattice Trace）'],
  ['compact', '压缩上下文，保留约束与回执', '[压缩要求]'],
  ['context', '清理或重置上下文', 'clean | reset'],
  ['new', '保留源码，开始新 Trial'],
  ['fork', '从历史消息创建分支', '[entry]'],
  ['clone', '复制当前上下文到新分支'],
  ['tree', '浏览对话树并选择分支'],
  ['model', '查看或切换允许的模型路由', '[route]'],
  ['thinking', '查看或切换允许的推理等级', '[level]'],
  ['scoped-models', '查看允许的模型路由'],
  ['settings', '查看会话设置'],
  ['trust', '查看已冻结的任务指令'],
  ['reload', '重新加载已冻结的资源'],
  ['resume', '恢复历史会话；默认当前项目，--all 查看全部', '[ID或标题] [--all]'],
  ['import', '导入有源码检查点的归档会话', '[path]'],
  ['export', '导出对话到运行目录'],
  ['copy', '复制最近一条回复'],
  ['name', '命名会话', '[name]'],
  ['session', '查看原生会话信息'],
  ['changelog', '查看 Pi 更新记录'],
  ['hotkeys', '查看快捷键'],
  ['login', '查看宿主凭据配置方式'],
  ['logout', '查看宿主凭据配置方式'],
  ['detach', '退出界面，允许活跃 Goal 在后台继续'],
  ['quit', '退出并按当前策略暂停或继续'],
].map(([name, description, argumentHint]) => Object.freeze({ name, description, ...(argumentHint ? { argumentHint } : {}) })));

export const PRODUCT_COMMAND_NAMES = new Set(PRODUCT_COMMANDS.map(item => '/' + item.name));

/** Codex CLI semantics: only exact lifecycle words are commands; other text is the objective. */
export function parseGoalCommand(text) {
  const objective = text.replace(/^\/goal(?:\s+|$)/, '').trim();
  if (!objective) return { operation: 'status' };
  if (['pause', 'resume', 'clear'].includes(objective)) return { operation: objective };
  // KernelBridge-specific exit policy, retained for compatibility.
  if (/^on-detach(?:\s|$)/.test(objective)) {
    const value = objective.slice('on-detach'.length).trim();
    if (!['pause', 'continue'].includes(value)) throw new Error('用法：/goal on-detach pause|continue');
    return { operation: 'on-detach', value };
  }
  return { operation: 'set', objective };
}

/** One help table for both modes; Claude Code and Codex show commands, not policy. */
const HELP_ROWS = {
  project: [
    ['/new  /clear', '新对话，保留工作文件'],
    ['/resume', '恢复历史会话'],
    ['/agents', '子代理类型与运行记录'],
    ['/trace', '浏览器中查看实时轨迹'],
    ['/model', '切换模型'],
    ['/compact', '压缩上下文'],
    ['/goal 目标内容', '持续执行直到完成'],
    ['/goal pause | resume | clear', '暂停、恢复、清除目标'],
    ['/name 标题', '命名当前会话'],
    ['/quit  /exit', '退出'],
  ],
  task: [
    ['/goal 目标内容', '持续执行直到完成'],
    ['/goal pause | resume | clear', '暂停、恢复、清除目标'],
    ['/context reset  /new', '开始新 Trial'],
    ['/fork  /tree', '分支对话或浏览历史'],
    ['/resume  /import', '恢复带源码检查点的会话'],
    ['/agents', '子代理类型与运行记录'],
    ['/trace', '浏览器中查看实时轨迹'],
    ['/compact', '压缩上下文'],
    ['/model  /settings', '模型路由与会话设置'],
    ['/detach', '退出并允许 Goal 继续'],
    ['/quit', '退出并按策略暂停'],
  ],
};

// Short lines so the block still fits a 24-row, 48-column terminal.
const KEYS = ['esc 中断 · ctrl+o 展开工具输出 · ! 运行 shell', '!! 不进上下文 · 输入 / 查看全部可用命令'];

/** Two aligned columns; CJK counts as two columns, and a wider command keeps its own line. */
export function commandHelp(mode = 'project') {
  const rows = HELP_ROWS[mode === 'project' ? 'project' : 'task'];
  const column = Math.min(22, Math.max(...rows.map(([command]) => visibleWidth(command))));
  const pad = (text, width) => text + ' '.repeat(Math.max(0, width - visibleWidth(text)));
  return ['命令',
    ...rows.flatMap(([command, text]) => visibleWidth(command) > column
      ? ['  ' + command, pad('', column + 4) + text]
      : ['  ' + pad(command, column + 2) + text]),
    '快捷键', ...KEYS.map(line => '  ' + line)].join('\n');
}
