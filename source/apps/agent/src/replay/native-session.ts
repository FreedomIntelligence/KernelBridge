import { readFile, stat } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { isAbsolute } from 'node:path';
import type {
  AgentSession, AgentSessionRuntime, FileEntry, SessionEntry, SessionManager,
} from '@earendil-works/pi-coding-agent';

export type NativeNavigation =
  | { readonly kind: 'restore'; readonly sessionFile: string }
  | { readonly kind: 'tree'; readonly entryId: string }
  | { readonly kind: 'fork'; readonly entryId: string; readonly position: 'before' | 'at' }
  | { readonly kind: 'clone' };

export interface NativeSessionIdentity {
  readonly sessionId: string;
  readonly sessionFile: string | undefined;
  readonly leafId: string | null;
  readonly cwd: string;
}

export interface NativeNavigationPermit {
  readonly request: NativeNavigation;
  readonly source: NativeSessionIdentity;
  readonly target: NativeSessionIdentity;
}

export interface NativeNavigationBindings {
  /** The original owner admits the operation, or throws; an unknown outcome must stay unknown. */
  readonly admit: (operation: NativeNavigationPermit) => Promise<void>;
  /** Reapply the explicitly admitted tool loadout, including after same-file tree navigation. */
  readonly rebind: (session: AgentSession) => Promise<void>;
}

export interface NativeNavigationResult extends NativeSessionIdentity {
  readonly cancelled: boolean;
  readonly editorText?: string;
  readonly selectedText?: string;
}

export function nativeSessionIdentity(session: AgentSession): NativeSessionIdentity {
  return managerIdentity(session.sessionManager);
}

function managerIdentity(manager: SessionManager): NativeSessionIdentity {
  return { sessionId: manager.getSessionId(), sessionFile: manager.getSessionFile(),
    leafId: manager.getLeafId(), cwd: manager.getCwd() };
}

function assertIdle(session: AgentSession): void {
  if (!session.isIdle || session.isStreaming || session.isCompacting || session.isRetrying ||
      session.pendingMessageCount !== 0) {
    throw new Error('Native navigation requires an idle session with no queued input');
  }
}

/** Scan the native branch without projecting away unresolved exchanges or rewriting history. */
function assertCompleteExchange(entries: readonly SessionEntry[]): void {
  const pending = new Set<string>();
  for (const entry of entries) {
    if (entry.type !== 'message') continue;
    if (entry.message.role === 'assistant') {
      for (const block of entry.message.content) {
        if (block.type === 'toolCall') pending.add(block.id);
      }
    } else if (entry.message.role === 'toolResult') {
      pending.delete(entry.message.toolCallId);
    }
  }
  if (pending.size > 0) {
    throw new Error(`Native navigation refuses unresolved tool results: ${[...pending].join(', ')}`);
  }
}

function selectedBoundary(manager: SessionManager, request: Exclude<NativeNavigation, { kind: 'restore' }>): string | null {
  if (request.kind === 'clone') {
    const leaf = manager.getLeafId();
    if (leaf === null) throw new Error('Cannot clone an empty native session');
    return leaf;
  }
  const selected = manager.getEntry(request.entryId);
  if (!selected) throw new Error(`Native navigation entry not found: ${request.entryId}`);
  if (request.kind === 'fork' && request.position === 'before') {
    if (selected.type !== 'message' || selected.message.role !== 'user') {
      throw new Error('Native fork before-position requires a user message');
    }
    return selected.parentId;
  }
  if (request.kind === 'tree' && (selected.type === 'custom_message' ||
      (selected.type === 'message' && selected.message.role === 'user'))) return selected.parentId;
  return selected.id;
}

function assertAppendOnlyTree(entries: readonly FileEntry[]): void {
  const seen = new Set<string>();
  for (const entry of entries.slice(1)) {
    if (!entry || entry.type === 'session' || typeof entry.id !== 'string' || entry.id.length === 0 ||
        seen.has(entry.id) || (entry.parentId !== null && !seen.has(entry.parentId))) {
      throw new Error('Native restore requires unique entry identities and previously recorded parents');
    }
    seen.add(entry.id);
  }
}

async function restoreTarget(sessionFile: string): Promise<{ manager: SessionManager; digest: string }> {
  const { CURRENT_SESSION_VERSION, parseSessionEntries, SessionManager } = await import('@earendil-works/pi-coding-agent');
  if (!isAbsolute(sessionFile)) throw new Error('Native restore requires an absolute session file');
  const info = await stat(sessionFile);
  if (!info.isFile() || info.size === 0 || info.size > 32 * 1024 * 1024) {
    throw new Error('Native restore requires a nonempty session file of at most 32 MiB');
  }
  const bytes = await readFile(sessionFile);
  if (bytes.length > 32 * 1024 * 1024) throw new Error('Native restore session grew beyond 32 MiB');
  const content = bytes.toString('utf8');
  const entries = parseSessionEntries(content);
  if (entries.length !== content.split('\n').filter(line => line.trim() !== '').length) {
    throw new Error('Native restore refuses malformed or truncated JSONL entries');
  }
  const header = entries[0];
  if (header?.type !== 'session' || header.version !== CURRENT_SESSION_VERSION ||
      typeof header.id !== 'string' || !header.id || typeof header.cwd !== 'string' || !isAbsolute(header.cwd)) {
    throw new Error('Native restore requires a current-version Pi session with an explicit cwd');
  }
  assertAppendOnlyTree(entries);
  // In-memory public parsing cannot migrate, initialize or rewrite the supplied file.
  return { manager: SessionManager.inMemory(header.cwd, undefined, entries),
    digest: createHash('sha256').update(bytes).digest('hex') };
}

/** Native public parsing validates a bounded complete file without rewriting it. */
export async function inspectNativeSessionFile(sessionFile: string): Promise<NativeSessionIdentity & { digest: string }> {
  const { manager, digest } = await restoreTarget(sessionFile);
  assertCompleteExchange(manager.getBranch());
  return { ...managerIdentity(manager), sessionFile, digest };
}

async function inspectTarget(runtime: AgentSessionRuntime, request: NativeNavigation) {
  const restored = request.kind === 'restore' ? await restoreTarget(request.sessionFile) : undefined;
  const manager = restored?.manager ?? runtime.session.sessionManager;
  const boundary = request.kind === 'restore' ? manager.getLeafId() : selectedBoundary(manager, request);
  assertCompleteExchange(boundary === null ? [] : manager.getBranch(boundary));
  return { identity: { ...managerIdentity(manager), leafId: boundary,
    sessionFile: request.kind === 'restore' ? request.sessionFile : manager.getSessionFile() },
    digest: restored?.digest };
}

async function performNavigation(runtime: AgentSessionRuntime, request: NativeNavigation) {
  if (request.kind === 'restore') return runtime.switchSession(request.sessionFile);
  if (request.kind === 'tree') return runtime.session.navigateTree(request.entryId, { summarize: false });
  if (request.kind === 'fork') return runtime.fork(request.entryId, { position: request.position });
  return runtime.fork(runtime.session.sessionManager.getLeafId()!, { position: 'at' });
}

/** Caller holds exclusive session control through admission, native hooks and rebind. No retries. */
export async function navigateNativeSession(
  runtime: AgentSessionRuntime, request: NativeNavigation, bindings: NativeNavigationBindings,
): Promise<NativeNavigationResult> {
  assertIdle(runtime.session);
  assertCompleteExchange(runtime.session.sessionManager.getBranch());
  const source = nativeSessionIdentity(runtime.session);
  const target = await inspectTarget(runtime, request);
  await bindings.admit({ request, source, target: target.identity });
  assertIdle(runtime.session);
  if (JSON.stringify(nativeSessionIdentity(runtime.session)) !== JSON.stringify(source) ||
      JSON.stringify(await inspectTarget(runtime, request)) !== JSON.stringify(target)) {
    throw new Error('Native navigation source or target changed during owner admission');
  }
  const result = await performNavigation(runtime, request);
  if (!result.cancelled) await bindings.rebind(runtime.session);
  return { ...result, ...nativeSessionIdentity(runtime.session) };
}
