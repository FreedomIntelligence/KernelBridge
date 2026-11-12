/** Pi owns tool semantics; this adapter owns the admitted filesystem boundary. */
import * as fs from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { dirname, isAbsolute, join, relative, resolve, sep } from 'node:path';
import { tmpdir } from 'node:os';
import { withProductRenderers } from './tool-render.mjs';

const RESERVED = new Set(['.git', '.kernelagent', '.evaluations']);
const MAX_FILE = 262144;
const MAX_TREE = 16 * 1024 * 1024;
const hash = data => createHash('sha256').update(data).digest('hex');
const under = (path, parent) => path === parent || path.startsWith(parent + '/');
const quote = value => "'" + value.replaceAll("'", "'\\''") + "'";
const asciiCasePattern = value => [...value].map(char => /[a-z]/i.test(char)
  ? `[${char.toLowerCase()}${char.toUpperCase()}]` : char.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('');

export function workspaceTools(sdk, capability) {
  const root = resolve(capability.root);
  // Ordinary codegen uses Pi's native project tools. Isolated experiment mode
  // retains the snapshot/writeback boundary below. Both paths require the same
  // host admission and original tool-result settlement in native.mjs.
  if (capability.mode === 'project') {
    const factories = { read: sdk.createReadToolDefinition, write: sdk.createWriteToolDefinition, edit: sdk.createEditToolDefinition,
      bash: sdk.createBashToolDefinition, grep: sdk.createGrepToolDefinition, find: sdk.createFindToolDefinition, ls: sdk.createLsToolDefinition };
    return (capability.tools ?? Object.keys(factories)).filter(name => factories[name]).map(name => ({
      ...factories[name](root, name === 'bash' ? { exposeSessionEnvironment: false,
        spawnHook: context => ({ ...context, env: { ...context.env, ...(capability.home ? { HOME: capability.home } : {}) } }) } : undefined),
      exposure: 'model-only' })).map(withProductRenderers);
  }
  const writable = capability.writable_paths;
  const readonly = capability.readonly_paths ?? ['reference'];
  // New hosts supply the actual Darwin volume pathconf result. Older macOS
  // capabilities fail conservatively; Linux keeps distinct case spellings.
  if (capability.case_sensitive !== undefined && typeof capability.case_sensitive !== 'boolean') {
    throw new Error('Workspace filesystem case sensitivity must be a host-supplied boolean');
  }
  const caseSensitive = capability.case_sensitive ?? (process.platform !== 'darwin');
  const pathKey = name => caseSensitive ? name : name.normalize('NFD').toUpperCase().toLowerCase();
  const reservedNames = new Set([...RESERVED].map(pathKey));
  for (const paths of [writable, readonly]) {
    if (paths == null) continue;
    if (!Array.isArray(paths) || paths.some(path => typeof path !== 'string' || !path || isAbsolute(path)
        || path.includes('\\') || path.includes('\0') || path.split('/').some(part => !part || part === '.' || part === '..'))) {
      throw new Error('Workspace policy requires normalized relative paths');
    }
  }
  const readonlyNames = readonly.map(pathKey);

  function local(path) {
    const absolute = resolve(root, path);
    const name = relative(root, absolute).split(sep).join('/');
    if (name === '..' || name.startsWith('../') || isAbsolute(name) || name.includes('\\') || name.includes('\0')
        || reservedNames.has(pathKey(name.split('/')[0]))) throw new Error('Path is outside the admitted workspace');
    return { absolute, name };
  }
  function canWrite(name) {
    // A regular-file write must not replace an ancestor of a protected path.
    const key = pathKey(name);
    return name && !readonlyNames.some(p => under(key, p) || under(p, key))
      && (writable == null || writable.includes(name));
  }
  async function checked(path, write = false) {
    const selected = local(path);
    if (write && !canWrite(selected.name)) throw new Error(`Read-only task path: ${selected.name}`);
    let cursor = root;
    for (const part of [null, ...selected.name.split('/').filter(Boolean)]) {
      try {
        const entries = part ? await fs.readdir(cursor) : null;
        if (part) cursor = join(cursor, part);
        const stat = await fs.lstat(cursor);
        if (part && !entries.includes(part)) throw new Error('Workspace path uses a non-canonical filesystem alias');
        if (stat.isSymbolicLink()) throw new Error('Workspace symlinks are forbidden');
      } catch (error) { if (error.code === 'ENOENT') break; throw error; }
    }
    return selected.absolute;
  }
  async function readFile(path) {
    const target = await checked(path);
    const stat = await fs.stat(target);
    if (!stat.isFile() || stat.size > MAX_FILE) throw new Error('File missing, non-regular, or too large');
    return fs.readFile(target);
  }
  async function writeFile(path, content, expected) {
    const target = await checked(path, true);
    if (Buffer.byteLength(content) > MAX_FILE) throw new Error('File too large');
    if (expected !== undefined) {
      let current = null;
      try { current = hash(await readFile(target)); } catch (error) { if (error.code !== 'ENOENT') throw error; }
      if (current !== expected) throw new Error('Workspace changed during the operation; inspect before retrying');
    }
    await fs.mkdir(dirname(target), { recursive: true });
    await checked(target, true);
    let mode = 0o644;
    try { mode = (await fs.stat(target)).mode & 0o777; } catch (error) { if (error.code !== 'ENOENT') throw error; }
    const temp = await fs.mkdtemp(join(dirname(target), '.pi-write-'));
    try {
      const file = join(temp, 'content');
      const handle = await fs.open(file, 'wx', mode);
      try { await handle.writeFile(content); await handle.sync(); } finally { await handle.close(); }
      await fs.rename(file, target);
    } finally { await fs.rm(temp, { recursive: true, force: true }); }
  }
  async function inventory(directory, trusted = true) {
    const files = new Map(); let bytes = 0;
    async function walk(current, prefix = '') {
      for (const entry of await fs.readdir(current, { withFileTypes: true })) {
        const name = prefix + entry.name;
        if (!prefix && reservedNames.has(pathKey(entry.name))) continue;
        if (entry.isSymbolicLink()) {
          if (!trusted) throw new Error('Shell output contains a symlink');
          continue;
        }
        const path = join(current, entry.name);
        if (entry.isDirectory()) await walk(path, name + '/');
        else if (entry.isFile()) {
          const stat = await fs.stat(path);
          if (stat.size > MAX_FILE) throw new Error(`Workspace file too large: ${name}`);
          bytes += stat.size;
          if (files.size >= 4096 || bytes > MAX_TREE) throw new Error('Workspace snapshot exceeds its bound');
          files.set(name, await fs.readFile(path));
        } else throw new Error('Workspace contains a special file');
      }
    }
    await walk(directory); return files;
  }
  async function withSnapshot(execute, writeback = false) {
    await checked(root);
    const before = await inventory(root);
    const directory = await fs.mkdtemp(join(tmpdir(), 'kernelcat-workspace-'));
    const container = await fs.realpath(directory);
    const scratch = join(container, 'workspace');
    const temporary = join(container, 'tmp');
    try {
      await fs.mkdir(scratch);
      await fs.mkdir(temporary);
      for (const [name, data] of before) {
        await fs.mkdir(dirname(join(scratch, name)), { recursive: true });
        await fs.writeFile(join(scratch, name), data);
        await fs.chmod(join(scratch, name), (await fs.stat(join(root, name))).mode & 0o777);
      }
      const result = await execute(scratch, before, temporary);
      if (writeback && result.exitCode === 0) {
        const after = await inventory(scratch, false);
        const changes = [];
        for (const name of new Set([...before.keys(), ...after.keys()])) {
          if (!before.has(name) || !after.has(name) || !before.get(name).equals(after.get(name))
              || ((await fs.stat(join(root,name))).mode & 0o777) !== ((await fs.stat(join(scratch,name))).mode & 0o777)) changes.push(name);
        }
        // Validate the complete diff before applying any of it.
        for (const name of changes) {
          await checked(name, true);
          let current = null;
          try { current = hash(await readFile(name)); } catch (error) { if (error.code !== 'ENOENT') throw error; }
          if (current !== (before.has(name) ? hash(before.get(name)) : null)) throw new Error('Workspace changed during bash');
        }
        for (const name of changes) {
          if (after.has(name)) {
            await writeFile(name, after.get(name), before.has(name) ? hash(before.get(name)) : null);
            await fs.chmod(join(root, name), (await fs.stat(join(scratch, name))).mode & 0o777);
          }
          else await fs.unlink(await checked(name, true));
        }
      }
      return result;
    } finally { await fs.rm(directory, { recursive: true, force: true }); }
  }

  const read = sdk.createReadToolDefinition(root, { operations: {
    readFile, access: async p => { await readFile(p); }, detectImageMimeType: async () => null,
  } });
  const write = sdk.createWriteToolDefinition(root, { operations: {
    mkdir: async () => {}, writeFile,
  } });
  write.description += ' Create durable helper scripts and scratch files with workspace-relative paths. Direct writes cannot access host /tmp; use bash with $TMPDIR for disposable files within one shell call. Readonly task documents, references and controller paths remain protected.';
  const edits = new Map();
  const edit = sdk.createEditToolDefinition(root, { operations: {
    access: async p => { await checked(p, true); },
    readFile: async p => { const data = await readFile(p); edits.set(p, hash(data)); return data; },
    writeFile: async (p, content) => {
      const expected = edits.get(p); edits.delete(p);
      if (expected === undefined) throw new Error('Edit has no original file snapshot');
      await writeFile(p, content, expected);
    },
  } });
  const ls = sdk.createLsToolDefinition(root, { operations: {
    exists: async p => { try { await fs.stat(await checked(p)); return true; } catch (error) { if (error.code === 'ENOENT') return false; throw error; } },
    stat: async p => fs.stat(await checked(p)),
    readdir: async p => {
      const selected = await checked(p); const entries = await fs.readdir(selected, { withFileTypes: true });
      return entries.filter(e => !e.isSymbolicLink() && !(selected === root && reservedNames.has(pathKey(e.name)))).map(e => e.name);
    },
  } });
  function search(factory) {
    const definition = factory(root);
    return { ...definition, execute: async (id, args, signal, update, context) => {
      const selected = await checked(args.path || root);
      return withSnapshot(scratch => factory(scratch).execute(id,
        { ...args, path: join(scratch, relative(root, selected)) }, signal, update,
        context ? { ...context, cwd: scratch } : undefined));
    } };
  }
  const shell = sdk.createLocalBashOperations({ shellPath: '/bin/bash' });
  const bash = sdk.createBashToolDefinition(root, { exposeSessionEnvironment: false, operations: {
    exec: async (command, _cwd, options) => withSnapshot(async (scratch, original, temporary) => {
      const protectedFiles = [...original.keys()].filter(name => !canWrite(name));
      // Include absent readonly and controller paths: denying only existing
      // files would permit first-time creation at these policy boundaries.
      const protections = [...new Set([...RESERVED, ...readonly, ...protectedFiles])];
      const protectedRoots = protections.filter(name => !protections.some(parent => parent !== name && under(name, parent)));
      const protectedAncestors = new Set([scratch]);
      for (const name of protectedRoots) {
        for (let parent = dirname(join(scratch, name)); parent !== scratch; parent = dirname(parent)) {
          protectedAncestors.add(parent);
        }
      }
      const ancestors = [...protectedAncestors].sort((a, b) => a.split(sep).length - b.split(sep).length);
      // Materialize only disposable directories. A missing parent must not be
      // replaceable by a regular file that shadows a protected descendant.
      for (const parent of ancestors) await fs.mkdir(parent, { recursive: true });
      let isolated;
      if (process.platform === 'darwin') {
        const system = ['/usr', '/bin', '/sbin', '/System/Library', '/System/Volumes/Preboot', '/Library', '/Applications', '/dev', '/opt', '/private/etc', '/private/var/db'];
        const profile = '(version 1)(deny default)(allow process*)(allow sysctl-read)(allow mach-lookup)(allow file-read-metadata)'
          + '(allow file-read* (literal "/") '
          + [...system, scratch, temporary].map(p => `(subpath ${JSON.stringify(p)})`).join(' ') + ')'
          + `(allow file-write* (subpath ${JSON.stringify(scratch)}) (subpath ${JSON.stringify(temporary)}) (literal "/dev/null"))`
          + protectedRoots.map(name => `(deny file-write* (literal ${JSON.stringify(join(scratch, name))}) (subpath ${JSON.stringify(join(scratch, name))}))`).join('')
          + (caseSensitive ? '' : protectedRoots.map(name => `(deny file-write* (regex #${JSON.stringify('^' + asciiCasePattern(join(scratch, name)) + '(/.*)?$')}))`).join(''))
          // Seatbelt's subpath rule alone does not protect creation of the
          // exact root; ancestor unlink denies also prevent rename bypasses.
          + ancestors.map(path => `(deny file-write-unlink (literal ${JSON.stringify(path)}))`).join('')
          + (caseSensitive ? '' : ancestors.map(path => `(deny file-write-unlink (regex #${JSON.stringify('^' + asciiCasePattern(path) + '$')}))`).join(''))
          // These macOS operations can mutate files through readonly handles.
          + '(deny system-fcntl (fcntl-command 80 110))';
        isolated = ['/usr/bin/sandbox-exec', '-p', profile, '/usr/bin/env', '-i',
          'PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin', 'LANG=en_US.UTF-8', `TMPDIR=${temporary}`, `HOME=${scratch}`,
          '/bin/bash', '--noprofile', '--norc', '-e', '-o', 'pipefail', '-c', command].map(quote).join(' ');
      } else if (process.platform === 'linux') {
        // No unsandboxed fallback: provision bubblewrap in the runner image.
        const executable = process.env.KERNELCAT_BWRAP ?? 'bwrap';
        if (executable !== 'bwrap' && !isAbsolute(executable)) throw new Error('KERNELCAT_BWRAP must be an absolute host path');
        const argv = [executable, '--unshare-all', '--die-with-parent', '--new-session'];
        for (const path of ['/usr', '/bin', '/sbin', '/lib', '/lib64']) {
          try { await fs.stat(path); argv.push('--ro-bind', path, path); } catch (error) { if (error.code !== 'ENOENT') throw error; }
        }
        // An empty /proc also works in nested runners that prohibit proc mounts;
        // never bind the host's /proc (which can expose other processes/files).
        argv.push('--dir', '/proc', '--dev', '/dev', '--tmpfs', '/tmp', '--setenv', 'TMPDIR', '/tmp', '--bind', scratch, '/workspace');
        // Each protected ancestor is a mount point, so Linux cannot rename it
        // and move protected children outside their readonly mount locations.
        for (const parent of ancestors) {
          if (parent !== scratch) argv.push('--bind', parent, join('/workspace', relative(scratch, parent)));
        }
        for (const name of protectedRoots) {
          const path = join(scratch, name);
          try { await fs.lstat(path); }
          catch (error) {
            if (error.code !== 'ENOENT') throw error;
            // Empty readonly placeholders exist in this shell snapshot only;
            // they have no regular-file entry to copy back to the workspace.
            await fs.mkdir(path);
          }
          argv.push('--ro-bind', path, join('/workspace', name));
        }
        argv.push('--chdir', '/workspace',
          '/usr/bin/env', '-i', 'PATH=/usr/local/bin:/usr/bin:/bin', 'LANG=C.UTF-8', 'TMPDIR=/tmp', 'HOME=/workspace',
          '/bin/bash', '--noprofile', '--norc', '-e', '-o', 'pipefail', '-c', command);
        isolated = argv.map(quote).join(' ');
      } else throw new Error('No isolated bash backend on this platform');
      return shell.exec(isolated, scratch, { ...options, env: { PATH: '/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin', LANG: 'en_US.UTF-8', TMPDIR: temporary } });
    }, true),
  } });
  bash.description += ' Commands run in a fresh isolated workspace snapshot with no network. Only successful authorized regular-file changes persist (256 KiB per file, 16 MiB and 4096 files per snapshot). TMPDIR is disposable. Failed or timed-out commands do not write changes back; inspect their output before retrying.';
  return [read, write, edit, ls, search(sdk.createFindToolDefinition), search(sdk.createGrepToolDefinition), bash]
    .filter(tool => capability.tools.includes(tool.name))
    .map(tool => withProductRenderers({ ...tool, exposure: 'model-only' }));
}
