// Pi session files on disk: the resumed branch of a file, and cached reads of
// subagent session files named by pi-subagents results.
import { readFileSync, statSync } from 'node:fs';
import { isAbsolute } from 'node:path';
import type { Json } from './trace-core.ts';

/** Header, current branch and every entry of a session file; a torn last line is ignored until complete. */
export function sessionBranch(text: string): { header: Json; entries: Json[]; tree: Json[] } {
  const complete = text.slice(0, text.lastIndexOf('\n') + 1);
  const rows: Json[] = [];
  for (const line of complete.split('\n')) {
    if (!line.trim()) continue;
    try { rows.push(JSON.parse(line)); } catch { /* a corrupt line is skipped; Pi itself refuses to load it */ }
  }
  const header = rows.find(row => row.type === 'session') ?? {};
  const entries = rows.filter(row => row.type !== 'session' && typeof row.id === 'string');
  const byId = new Map(entries.map(entry => [entry.id, entry]));
  const branch: Json[] = [];
  for (let entry = entries.at(-1); entry; entry = entry.parentId ? byId.get(entry.parentId) : undefined) {
    branch.push(entry);
    if (branch.length > entries.length) break; // a parent cycle is not a valid session
  }
  return { header, entries: branch.reverse(), tree: entries };
}

const cache = new Map<string, { mtimeMs: number; size: number; entries: Json[] }>();

/** Entries of a subagent's own session file, re-read only when the file changed. */
export function childSessionEntries(result: Json): Json[] | null {
  const path = result?.sessionFile;
  if (typeof path !== 'string' || !isAbsolute(path) || !path.endsWith('.jsonl')) return null;
  try {
    const info = statSync(path);
    const known = cache.get(path);
    if (known && known.mtimeMs === info.mtimeMs && known.size === info.size) return known.entries;
    const entries = sessionBranch(readFileSync(path, 'utf8')).entries;
    cache.set(path, { mtimeMs: info.mtimeMs, size: info.size, entries });
    return entries;
  } catch { return null; }
}
