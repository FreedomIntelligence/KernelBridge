import { constants } from 'node:fs';
import { open } from 'node:fs/promises';
import { homedir } from 'node:os';
import { join } from 'node:path';

/** Credentials stay outside project configuration and frozen session artifacts. */
export async function loadCodegenCredentials(providers) {
  const names = [...new Set(Object.values(providers).map(provider => provider.api_key_env))]
    .filter(name => name && !process.env[name]?.trim());
  if (!names.length) return;
  const path = join(process.env.KERNELBRIDGE_HOME ?? join(homedir(), '.kernelbridge'), 'credentials.json');
  let file;
  try { file = await open(path, constants.O_RDONLY | constants.O_NOFOLLOW); }
  catch (error) { if (error.code === 'ENOENT') return; throw error; }
  try {
    const stat = await file.stat();
    if (!stat.isFile() || stat.size > 65536 || stat.mode & 0o077 || stat.uid !== process.getuid?.()) {
      throw new Error('凭据文件必须归当前用户所有，权限为 0600：' + path);
    }
    const credentials = JSON.parse(await file.readFile('utf8'));
    for (const name of names) {
      if (typeof credentials[name] === 'string' && credentials[name].trim()) process.env[name] = credentials[name];
    }
  } finally { await file.close(); }
}
