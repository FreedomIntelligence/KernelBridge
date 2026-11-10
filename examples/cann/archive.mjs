/** Verify and snapshot archive bytes before using paths or publishing a restored copy. */
import { open, realpath } from 'node:fs/promises';
import { constants } from 'node:fs';
import { isAbsolute, join } from 'node:path';
import { createHash } from 'node:crypto';
export const digest = value => createHash('sha256').update(value).digest('hex');

export async function verifyArchive(root) {
  root = await realpath(root);
  async function bytes(name, limit) {
    const path = join(root, name);
    if (await realpath(path) !== path) throw new Error('Archive entry traverses a symlink: ' + name);
    const file = await open(path, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
    try {
      const info = await file.stat();
      if (!info.isFile() || info.size > limit) throw new Error('Invalid archive file: ' + name);
      const data = await file.readFile();
      if (data.length > limit) throw new Error('Archive file grew beyond limit: ' + name);
      return data;
    } finally { await file.close(); }
  }
  const manifestBytes = await bytes('manifest.json', 8 * 1024 * 1024);
  const manifest = JSON.parse(manifestBytes);
  if (manifest.schema_version !== 'kernelbridge.run-archive/v1' || !Array.isArray(manifest.files)
      || manifest.files.length < 1 || manifest.files.length > 20000) throw new Error('Expected a bounded run archive manifest');
  const files = new Map();
  let total = 0;
  for (const row of manifest.files) {
    if (typeof row.path !== 'string' || isAbsolute(row.path) || /[\\\x00-\x1f]/.test(row.path)
        || row.path.split('/').some(part => ['', '.', '..'].includes(part)) || files.has(row.path)
        || ['manifest.json', 'restore.json', 'continuation-inputs'].includes(row.path.split('/')[0])
        || !Number.isSafeInteger(row.bytes) || row.bytes < 0 || row.bytes > 512 * 1024 * 1024
        || typeof row.sha256 !== 'string' || !/^[a-f0-9]{64}$/.test(row.sha256)) throw new Error('Invalid archive file identity');
    total += row.bytes;
    if (total > 512 * 1024 * 1024) throw new Error('Archive exceeds 512 MiB');
    const data = await bytes(row.path, row.bytes);
    if (data.length !== row.bytes || digest(data) !== row.sha256) throw new Error('Archive hash mismatch: ' + row.path);
    files.set(row.path, data);
  }
  const sessions = manifest.indices?.pi_sessions;
  if (!Array.isArray(sessions) || !sessions.length || new Set(sessions).size !== sessions.length
      || sessions.some(name => !files.has(name))) throw new Error('Missing indexed Pi session');
  return { root, manifest, manifestBytes, files };
}
