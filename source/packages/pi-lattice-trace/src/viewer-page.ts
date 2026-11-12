// Assembles the live Lattice Trace page: the vendored viewer, the initial trace
// as JSON, and the live adapter injected just before the viewer boots.
import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import type { Trace } from './trace-core.ts';

const VIEWER = fileURLToPath(new URL('../viewer/lattice-viewer.html', import.meta.url));
const ADAPTER = fileURLToPath(new URL('../viewer/live-adapter.js', import.meta.url));
// The vendored viewer boots with these two statements; the adapter must run first.
const BOOT = '\nloadSession(DS_INV);\nreadHash();\n';
// Demo-only text and context in the vendored viewer, replaced for live sessions.
// Each anchor must occur exactly once, so a changed viewer fails loudly.
const TRANSFORMS: [string, string][] = [
  ["  if (k === MAIN && DS.id === 'invoice') L =", "  if (DS.live) L = DS.baseCtx(k).map(function(b){ return newSeg(b[0], b[1], b[2], null, k).id; });\n  else if (k === MAIN && DS.id === 'invoice') L ="],
  ["'<span class=\"hint\">示例会话 · 分支 '", "'<span class=\"hint\">' + (DS && DS.live ? '实时会话' : '示例会话') + ' · 分支 '"],
  ['<p class="ov-k">三个分支 · 点选切换</p>', '<p class="ov-k">\' + (DS.live ? \'会话\' : \'三个分支 · 点选切换\') + \'</p>'],
  ["var cfg = '<div class=\"cfg\"><b>Harness · DSH standard</b>", "var cfg = DS.live ? DS.configHTML() : '<div class=\"cfg\"><b>Harness · DSH standard</b>"],
  ['<button class="btn" type="button" id="actFork">从这一步 fork</button>', "' + (DS.live ? '' : '<button class=\"btn\" type=\"button\" id=\"actFork\">从这一步 fork</button>') + '"],
];
// Web fonts the viewer links; offline pages fall back to the system fonts in its own stacks.
const FONT_LINKS = [
  '<link rel="preconnect" href="https://fonts.googleapis.com">\n',
  '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>\n',
  '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Geist:wght@300;400;500;600&family=Geist+Mono:wght@400;500&display=swap">\n',
];

export interface PageOptions { title?: string; offline?: boolean }

function replaceOnce(body: string, anchor: string, replacement: string): string {
  const first = body.indexOf(anchor);
  if (first < 0 || body.indexOf(anchor, first + 1) >= 0) throw new Error('Unsupported Lattice viewer: transform anchor not found exactly once: ' + anchor.slice(0, 60));
  return body.slice(0, first) + replacement + body.slice(first + anchor.length);
}

export async function viewerPage(trace: Trace, { title = 'KernelBridge · 实时轨迹', offline = false }: PageOptions = {}): Promise<string> {
  const [html, adapter] = await Promise.all([readFile(VIEWER, 'utf8'), readFile(ADAPTER, 'utf8')]);
  const at = html.indexOf(BOOT); const script = html.indexOf('<script>');
  if (at < 0 || html.indexOf(BOOT, at + 1) >= 0 || script < 0 || script > at) throw new Error('Unsupported Lattice viewer: boot sequence not found exactly once');
  const data = JSON.stringify(trace).replace(/</g, '\\u003c');
  let body = html.slice(script, at);
  for (const [anchor, replacement] of TRANSFORMS) body = replaceOnce(body, anchor, replacement);
  let head = html.slice(0, script).replace(/<title>[^<]*<\/title>/, () => `<title>${title.replace(/[&<>]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' })[c]!)}</title>`);
  if (offline) for (const link of FONT_LINKS) head = replaceOnce(head, link, '');
  // Initial data precedes the viewer script; the adapter runs inside it, just before boot.
  return head
    + `<script type="application/json" id="kb-trace">${data}</script>\n`
    + body + '\n/* kernelbridge live adapter */\n' + adapter + html.slice(at);
}
