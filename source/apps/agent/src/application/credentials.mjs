import { constants } from 'node:fs';
import { open } from 'node:fs/promises';
import { homedir } from 'node:os';
import { join } from 'node:path';

/** Credentials stay outside project configuration and frozen session artifacts. */
async function loadEnvironmentCredentials(requested) {
  if (requested.some(name => typeof name !== 'string' || !/^[A-Za-z_][A-Za-z0-9_]{0,127}$/.test(name))) {
    throw new Error('Credential references must be environment variable names');
  }
  const names = [...new Set(requested)].filter(name => !process.env[name]?.trim());
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

/** Only model routes and the explicitly admitted search tool receive credential loading.
 * Search keys must be independent of model credentials; never infer a search provider
 * or reuse a DeepSeek/OpenAI/Anthropic key because it is present on the host.
 */
export async function loadCodegenCredentials(providers, businessTools = []) {
  const modelNames = Object.values(providers).map(provider => provider.api_key_env).filter(Boolean);
  const searchNames = businessTools.filter(tool => tool.name === 'online_search').flatMap(tool => tool.env_allowlist ?? []);
  if (searchNames.some(name => modelNames.includes(name) || /DEEPSEEK|OPENAI|ANTHROPIC/i.test(name))) {
    throw new Error('Online search requires an independent search-provider api_key_env');
  }
  await loadEnvironmentCredentials([...modelNames, ...searchNames]);
  const modelValues = [...modelNames, 'DEEPSEEK_API_KEY', 'OPENAI_API_KEY', 'ANTHROPIC_API_KEY']
    .map(name => process.env[name]).filter(Boolean);
  if (searchNames.some(name => process.env[name] && modelValues.includes(process.env[name]))) {
    throw new Error('Online search credentials must not reuse model credential values');
  }
}

/** Values used only for redacting local diagnostic/event output, never configuration. */
export function configuredCredentialValues(providers, businessTools = []) {
  const names = [...Object.values(providers).map(provider => provider.api_key_env),
    ...businessTools.filter(tool => tool.name === 'online_search').flatMap(tool => tool.env_allowlist ?? [])];
  return [...new Set(names.map(name => process.env[name]).filter(Boolean))];
}
