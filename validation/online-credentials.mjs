import assert from 'node:assert/strict';
import { execFile } from 'node:child_process';
import { chmod, mkdtemp, readFile, rm, symlink, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { promisify } from 'node:util';
import test from 'node:test';

const execute = promisify(execFile);
const moduleUrl = new URL('../source/apps/agent/src/application/credentials.mjs', import.meta.url).href;

// Each case imports the real loader in a fresh process with only its dummy
// environment. No ambient credentials or user credential files enter the test.
async function fixture(t, options, body) {
  const directory = await mkdtemp(join(tmpdir(), 'kernelbridge-online-credentials-'));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const credentialPath = join(directory, 'credentials.json');
  let original;
  if (options.credentials !== undefined) {
    original = JSON.stringify(options.credentials);
    await writeFile(credentialPath, original, { flag: 'wx', mode: 0o600 });
    if (options.mode !== undefined) await chmod(credentialPath, options.mode);
  }
  if (options.link) {
    const target = join(directory, 'credential-target.json');
    await writeFile(target, JSON.stringify(options.link), { mode: 0o600, flag: 'wx' });
    await symlink(target, credentialPath);
  }
  const source = `import assert from 'node:assert/strict';
import { loadCodegenCredentials, configuredCredentialValues } from ${JSON.stringify(moduleUrl)};
${body}`;
  const result = await execute(process.execPath, ['--input-type=module', '-e', source], {
    env: { KERNELBRIDGE_HOME: directory, ...options.environment },
    timeout: 10000,
    maxBuffer: 65536,
  });
  assert.equal(result.stdout, '', 'credential loading must not print credential values');
  assert.equal(result.stderr, '', 'credential loading must not log credential values');
  if (original !== undefined) {
    assert.equal(await readFile(credentialPath, 'utf8'), original, 'loading must not rewrite the credential file');
  }
}

test('loads independent model/search references from a private credential file only for admitted tools', async t => {
  await fixture(t, { credentials: {
    KB_MODEL_KEY: 'dummy-model-file-value',
    KB_SEARCH_KEY: 'dummy-search-file-value',
    KB_OTHER_TOOL_KEY: 'dummy-unadmitted-tool-value',
    KB_FETCH_KEY: 'dummy-fetch-value',
    KB_READ_KEY: 'dummy-read-value',
    BRAVE_SEARCH_API_KEY: 'dummy-unrequested-default-value',
    DEEPSEEK_API_KEY: 'dummy-unrequested-deepseek-value',
  } }, `
const providers = { primary: { provider: 'example', api_key_env: 'KB_MODEL_KEY' } };
const tools = [
  { name: 'online_search', env_allowlist: ['KB_SEARCH_KEY'] },
  { name: 'some_business_tool', env_allowlist: ['KB_OTHER_TOOL_KEY'] },
  { name: 'online_fetch', env_allowlist: ['KB_FETCH_KEY'] },
  { name: 'online_read', env_allowlist: ['KB_READ_KEY'] },
];
const frozen = JSON.stringify({ providers, tools });
await loadCodegenCredentials(providers, tools);
assert.equal(process.env.KB_MODEL_KEY, 'dummy-model-file-value');
assert.equal(process.env.KB_SEARCH_KEY, 'dummy-search-file-value');
for (const name of ['KB_OTHER_TOOL_KEY', 'KB_FETCH_KEY', 'KB_READ_KEY', 'BRAVE_SEARCH_API_KEY', 'DEEPSEEK_API_KEY']) {
  assert.equal(process.env[name], undefined, name + ' must not be loaded automatically');
}
assert.deepEqual(configuredCredentialValues(providers, tools), ['dummy-model-file-value', 'dummy-search-file-value']);
assert.equal(JSON.stringify({ providers, tools }), frozen, 'credential bytes must not be inserted into configuration');
`);
});

test('explicit environment values override file values while missing independent references still load', async t => {
  await fixture(t, { credentials: {
    KB_MODEL_KEY: 'dummy-model-from-file', KB_SEARCH_KEY: 'dummy-search-from-file',
  }, environment: { KB_MODEL_KEY: 'dummy-model-from-environment' } }, `
const providers = { primary: { api_key_env: 'KB_MODEL_KEY' } };
const tools = [{ name: 'online_search', env_allowlist: ['KB_SEARCH_KEY'] }];
await loadCodegenCredentials(providers, tools);
assert.equal(process.env.KB_MODEL_KEY, 'dummy-model-from-environment');
assert.equal(process.env.KB_SEARCH_KEY, 'dummy-search-from-file');
assert.deepEqual(configuredCredentialValues(providers, tools), ['dummy-model-from-environment', 'dummy-search-from-file']);
`);
});

test('a fully supplied environment does not consult a credential file', async t => {
  await fixture(t, { credentials: { KB_MODEL_KEY: 'dummy-unused-model', KB_SEARCH_KEY: 'dummy-unused-search' }, mode: 0o644,
    environment: { KB_MODEL_KEY: 'dummy-env-model', KB_SEARCH_KEY: 'dummy-env-search' } }, `
await loadCodegenCredentials({ primary: { api_key_env: 'KB_MODEL_KEY' } },
  [{ name: 'online_search', env_allowlist: ['KB_SEARCH_KEY'] }]);
assert.equal(process.env.KB_MODEL_KEY, 'dummy-env-model');
assert.equal(process.env.KB_SEARCH_KEY, 'dummy-env-search');
`);
});

test('missing search credentials never fall back to an available DeepSeek credential', async t => {
  await fixture(t, { environment: { DEEPSEEK_API_KEY: 'dummy-deepseek-only' } }, `
const providers = { primary: { api_key_env: 'DEEPSEEK_API_KEY' } };
const tools = [{ name: 'online_search', env_allowlist: ['BRAVE_SEARCH_API_KEY'] }];
await loadCodegenCredentials(providers, tools);
assert.equal(process.env.BRAVE_SEARCH_API_KEY, undefined);
assert.equal(process.env.DEEPSEEK_API_KEY, 'dummy-deepseek-only');
assert.deepEqual(configuredCredentialValues(providers, tools), ['dummy-deepseek-only']);
`);
});

test('rejects a shared model/search environment name before loading it', async t => {
  await fixture(t, { credentials: { KB_SHARED_KEY: 'dummy-shared-value' } }, `
await assert.rejects(loadCodegenCredentials({ primary: { api_key_env: 'KB_SHARED_KEY' } },
  [{ name: 'online_search', env_allowlist: ['KB_SHARED_KEY'] }]), /independent search-provider api_key_env/);
assert.equal(process.env.KB_SHARED_KEY, undefined);
`);
});

test('rejects model-provider credential names even without a configured model route', async t => {
  await fixture(t, {}, `
for (const name of ['DEEPSEEK_API_KEY', 'OPENAI_API_KEY', 'ANTHROPIC_API_KEY', 'custom_deepseek_token']) {
  await assert.rejects(loadCodegenCredentials({}, [{ name: 'online_search', env_allowlist: [name] }]),
    /independent search-provider api_key_env/);
  assert.equal(process.env[name], undefined);
}
`);
});

test('rejects equal model/search values loaded under separate names', async t => {
  await fixture(t, { credentials: { KB_MODEL_KEY: 'dummy-equal-secret', KB_SEARCH_KEY: 'dummy-equal-secret' } }, `
await assert.rejects(loadCodegenCredentials({ primary: { api_key_env: 'KB_MODEL_KEY' } },
  [{ name: 'online_search', env_allowlist: ['KB_SEARCH_KEY'] }]), /must not reuse model credential values/);
`);
});

test('rejects reuse of ambient DeepSeek/OpenAI/Anthropic values under independent search names', async t => {
  await fixture(t, {}, `
for (const name of ['DEEPSEEK_API_KEY', 'OPENAI_API_KEY', 'ANTHROPIC_API_KEY']) {
  process.env[name] = 'dummy-' + name;
  process.env.KB_SEARCH_KEY = process.env[name];
  await assert.rejects(loadCodegenCredentials({}, [{ name: 'online_search', env_allowlist: ['KB_SEARCH_KEY'] }]),
    /must not reuse model credential values/);
  delete process.env[name];
  delete process.env.KB_SEARCH_KEY;
}
`);
});

test('redaction values include only declared model/search credentials and are deduplicated', async t => {
  await fixture(t, { environment: {
    KB_MODEL_KEY: 'dummy-model-redaction', KB_MODEL_ALIAS: 'dummy-model-redaction', KB_SEARCH_KEY: 'dummy-search-redaction',
    KB_OTHER_TOOL_KEY: 'dummy-other-not-requested', DEEPSEEK_API_KEY: 'dummy-ambient-not-requested',
  } }, `
const providers = { one: { api_key_env: 'KB_MODEL_KEY' }, alias: { api_key_env: 'KB_MODEL_ALIAS' }, missing: {} };
const tools = [{ name: 'online_search', env_allowlist: ['KB_SEARCH_KEY', 'KB_SEARCH_KEY'] },
  { name: 'other_business_tool', env_allowlist: ['KB_OTHER_TOOL_KEY'] }];
assert.deepEqual(configuredCredentialValues(providers, tools), ['dummy-model-redaction', 'dummy-search-redaction']);
`);
});

test('rejects unsafe credential-file permissions before loading any values', async t => {
  await fixture(t, { credentials: { KB_SEARCH_KEY: 'dummy-world-readable' }, mode: 0o644 }, `
await assert.rejects(loadCodegenCredentials({}, [{ name: 'online_search', env_allowlist: ['KB_SEARCH_KEY'] }]), /0600/);
assert.equal(process.env.KB_SEARCH_KEY, undefined);
`);
});

test('does not follow a credential-file symlink', async t => {
  await fixture(t, { link: { KB_SEARCH_KEY: 'dummy-symlink-secret' } }, `
await assert.rejects(loadCodegenCredentials({}, [{ name: 'online_search', env_allowlist: ['KB_SEARCH_KEY'] }]),
  error => error.code === 'ELOOP');
assert.equal(process.env.KB_SEARCH_KEY, undefined);
`);
});

test('rejects invalid environment references without interpreting them', async t => {
  await fixture(t, {}, `
for (const name of ['KEY;echo', 'KEY=value', '', '9KEY', 'K'.repeat(129)]) {
  await assert.rejects(loadCodegenCredentials({}, [{ name: 'online_search', env_allowlist: [name] }]),
    /Credential references must be environment variable names/);
}
`);
});
