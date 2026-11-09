/** Opt-in live Pi SDK/plugin check. All files and sessions are disposable. */
import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import { mkdtemp, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { parseArgs } from 'node:util';
import type { Api, Model, SimpleStreamOptions } from '@earendil-works/pi-ai';
import type { AgentSession, ExtensionAPI, ProviderConfig } from '@earendil-works/pi-coding-agent';

const MAX_MODEL_REQUESTS = 6;
const MAX_OUTPUT_TOKENS = 1024;
const MODEL_TIMEOUT_MS = 30_000;
const CHILD_TIMEOUT_MS = 45_000;
const PROBE_TIMEOUT_MS = 90_000;
const READER_NAME = 'kernelcat-smoke-reader';
const READER_TASK = 'Read probe.txt using read and return only its exact line.';
const apiKey = process.env.PI_SMOKE_API_KEY;

type ProbeApi = 'openai-responses' | 'openai-completions' | 'anthropic-messages';

interface ProbeConfiguration {
  baseUrl: string;
  model: string;
  api: ProbeApi;
  key: string;
  discover: boolean;
}

interface ProbeObservations {
  modelRequests: number;
  responses: { status: number }[];
  responseModels: Set<string>;
  modelFailures: string[];
  toolFailures: string[];
  started: number;
  completed: number;
  childMarkerObserved: boolean;
  extensionError?: string;
}

function configuration(): ProbeConfiguration {
  const { values } = parseArgs({ options: {
    'base-url': { type: 'string' },
    model: { type: 'string' },
    api: { type: 'string', default: 'openai-responses' },
    discover: { type: 'boolean', default: false },
  } });
  assert(apiKey, 'Set PI_SMOKE_API_KEY for this invocation.');
  assert(values['base-url'], 'Pass --base-url with the exact API base URL.');
  const endpoint = new URL(values['base-url']);
  assert(endpoint.protocol === 'https:' && !endpoint.username && !endpoint.password
    && !endpoint.search && !endpoint.hash, 'The API base must be credential-free HTTPS.');
  assert(values.api === 'openai-responses' || values.api === 'openai-completions'
    || values.api === 'anthropic-messages',
    '--api must be openai-responses, openai-completions or anthropic-messages.');
  assert(values.discover || values.model?.trim(), 'Pass the exact authorized --model ID.');
  return {
    baseUrl: endpoint.href.replace(/\/$/, ''),
    api: values.api,
    model: values.model ?? '',
    key: apiKey,
    discover: values.discover,
  };
}

async function discover(config: ProbeConfiguration): Promise<void> {
  const response = await fetch(`${config.baseUrl}/models`, {
    headers: { authorization: `Bearer ${config.key}` },
    redirect: 'error',
    signal: AbortSignal.timeout(20_000),
  });
  assert(response.ok, `Model catalogue returned HTTP ${response.status}.`);
  const body = await response.json() as { data?: { id?: string }[] };
  assert(Array.isArray(body.data), 'Model catalogue has no data array.');
  const models = body.data.map(entry => entry.id).filter(id => typeof id === 'string');
  assert(!models.some(id => id.includes(config.key)), 'Model catalogue echoed authentication material.');
  console.log(JSON.stringify({ model_ids: models, inference_called: false }));
}

function observations(): ProbeObservations {
  return {
    modelRequests: 0, responses: [], responseModels: new Set(), modelFailures: [],
    toolFailures: [], started: 0, completed: 0, childMarkerObserved: false,
  };
}

function modelDefinition(config: ProbeConfiguration, knownModel?: Model<Api>) {
  const matching = knownModel?.api === config.api && knownModel.baseUrl === config.baseUrl;
  return {
    id: config.model, name: config.model, input: ['text'] as ['text'], reasoning: true,
    contextWindow: 32768, maxTokens: MAX_OUTPUT_TOKENS,
    ...(matching ? { compat: knownModel.compat, thinkingLevelMap: knownModel.thinkingLevelMap } : {}),
    // Pricing is not discovered by this check. No monetary cost is reported.
    cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
  };
}

async function createProvider(
  config: ProbeConfiguration, observed: ProbeObservations, knownModel?: Model<Api>,
): Promise<ProviderConfig> {
  const responses = await import('@earendil-works/pi-ai/api/openai-responses');
  const completions = await import('@earendil-works/pi-ai/api/openai-completions');
  const anthropic = await import('@earendil-works/pi-ai/api/anthropic-messages');
  return {
    name: 'Explicit live smoke endpoint', api: config.api, baseUrl: config.baseUrl,
    apiKey: config.key, models: [modelDefinition(config, knownModel)],
    streamSimple(model, context, options) {
      assert(observed.modelRequests < MAX_MODEL_REQUESTS, 'Live smoke exceeded six model requests.');
      assert.equal(model.id, config.model, 'A model substitution was attempted.');
      assert.equal(model.api, config.api, 'A wire-protocol substitution was attempted.');
      observed.modelRequests++;
      const requestOptions: SimpleStreamOptions = {
        ...options, apiKey: config.key, maxTokens: MAX_OUTPUT_TOKENS,
        maxRetries: 0, timeoutMs: MODEL_TIMEOUT_MS, transport: 'sse',
        onResponse(response) { observed.responses.push({ status: response.status }); },
      };
      if (config.api === 'openai-responses') {
        return responses.streamSimple(model as Model<'openai-responses'>, context, requestOptions);
      }
      if (config.api === 'anthropic-messages') {
        return anthropic.streamSimple(model as Model<'anthropic-messages'>, context, requestOptions);
      }
      return completions.streamSimple(model as Model<'openai-completions'>, context, requestOptions);
    },
  };
}

async function createExtension(config: ProbeConfiguration, provider: ProviderConfig) {
  const { registerAgentViaEvents } = await import('pi-subagents/agents');
  const { registerSubagentCapabilityCeiling } = await import('pi-subagents/capability-ceiling');
  return (pi: ExtensionAPI): void => {
    const registrations: { dispose(): void }[] = [];
    pi.registerProvider('smoke', provider);
    registrations.push(registerAgentViaEvents({ pi, name: READER_NAME, definition: {
      description: 'Read the smoke fixture and return its exact line.',
      systemPrompt: 'Read probe.txt using read. Return only its exact line. Do no other work.',
      tools: ['read'], excludeTools: [], allowNestedSubagents: false,
      model: `smoke/${config.model}`, thinking: 'low', systemPromptMode: 'replace',
      inheritProjectContext: false, inheritGlobalContext: false, inheritSkills: false,
      defaultContext: 'fresh', defaultAsync: false, defaultTimeoutMs: CHILD_TIMEOUT_MS,
      skills: [], extensions: [], defaultReads: [], maxSubagentDepth: 1,
    } }));
    pi.on('session_start', (_event, context) => {
      registrations.push(registerSubagentCapabilityCeiling({
        sessionId: context.sessionManager.getSessionId(), source: 'kernelcat-smoke',
        ceiling: { allowedAgents: [READER_NAME], allowedTools: ['read'], denyExtensions: true },
      }));
    });
    pi.on('session_shutdown', () => {
      for (const registration of registrations.splice(0)) {
        registration.dispose();
      }
    });
  };
}

async function createSession(
  config: ProbeConfiguration, home: string, observed: ProbeObservations,
): Promise<AgentSession> {
  // Import only after HOME is isolated: SDK/plugin module initialization may read it.
  const sdk = await import('@earendil-works/pi-coding-agent');
  const ai = await import('@earendil-works/pi-ai');
  const { default: subagents } = await import('pi-subagents');
  const modelRuntime = await sdk.ModelRuntime.create({
    credentials: new ai.InMemoryCredentialStore(), modelsPath: null,
    allowModelNetwork: false, refreshOnCreate: false,
  });
  const provider = await createProvider(config, observed, modelRuntime.getModel('kimi-coding', config.model));
  modelRuntime.registerProvider('smoke', provider);
  const model = modelRuntime.getModel('smoke', config.model);
  assert(model, 'Explicit smoke model was not registered.');
  const settingsManager = sdk.SettingsManager.inMemory({
    compaction: { enabled: false }, retry: { enabled: false, maxRetries: 0 },
  });
  const agentDir = join(home, 'agent');
  const resourceLoader = new sdk.DefaultResourceLoader({
    cwd: home, agentDir, settingsManager, noExtensions: true, noSkills: true,
    noPromptTemplates: true, noThemes: true, noContextFiles: true,
    extensionFactories: [subagents, await createExtension(config, provider)],
    systemPrompt: 'This is an authorized subagent smoke test. Delegate exactly once, then report the child result.',
  });
  await resourceLoader.reload();
  assert.equal(resourceLoader.getExtensions().errors.length, 0, 'Required plugin failed to load.');
  const { session } = await sdk.createAgentSession({
    cwd: home, agentDir, modelRuntime, model, thinkingLevel: 'low', settingsManager,
    resourceLoader, sessionManager: sdk.SessionManager.inMemory(home),
    noTools: 'builtin', tools: ['subagent'],
  });
  return session;
}

function observeSession(
  session: AgentSession, observed: ProbeObservations, marker: string, key: string,
): void {
  session.subscribe(event => {
    if (event.type === 'tool_execution_start' && event.toolName === 'subagent') {
      observed.started++;
    }
    if (event.type === 'tool_execution_end' && event.toolName === 'subagent') {
      const result = JSON.stringify(event.result);
      if (event.isError) {
        observed.toolFailures.push(result.replaceAll(key, '[redacted]').slice(0, 1000));
      } else {
        observed.completed++;
        observed.childMarkerObserved ||= result.includes(marker);
      }
    }
    if (event.type === 'message_end' && event.message.role === 'assistant') {
      if (event.message.responseModel) {
        observed.responseModels.add(event.message.responseModel);
      }
      if (event.message.stopReason === 'error' || event.message.stopReason === 'aborted') {
        observed.modelFailures.push(event.message.errorMessage ?? event.message.stopReason);
      }
    }
  });
}

function reportObservations(observed: ProbeObservations, finalMarkerMatched: boolean): void {
  console.log(JSON.stringify({
    event: 'probe_observed', model_requests: observed.modelRequests,
    http_responses: observed.responses, response_models: [...observed.responseModels],
    subagents_started: observed.started, subagents_completed: observed.completed,
    child_marker_observed: observed.childMarkerObserved, final_marker_matched: finalMarkerMatched,
    tool_failures: observed.toolFailures,
  }));
}

function verifyProbe(observed: ProbeObservations, finalMarkerMatched: boolean): void {
  assert.equal(observed.extensionError, undefined, `Plugin failed: ${observed.extensionError}`);
  assert.equal(observed.modelFailures.length, 0, `Model request failed: ${observed.modelFailures.join('; ')}`);
  assert.equal(observed.started, 1, 'The parent did not launch exactly one subagent.');
  assert.equal(observed.completed, 1, 'The subagent did not complete successfully.');
  assert(finalMarkerMatched, 'The returned child marker did not match.');
}

async function closeSession(session: AgentSession): Promise<void> {
  try {
    await session.extensionRunner.emit({ type: 'session_shutdown', reason: 'quit' });
  } finally {
    session.dispose();
  }
}

async function runProbe(config: ProbeConfiguration, home: string): Promise<void> {
  const marker = `KERNELCAT_CHILD_${randomUUID()}`;
  await writeFile(join(home, 'probe.txt'), `${marker}\n`);
  const observed = observations();
  const session = await createSession(config, home, observed);
  let deadline: ReturnType<typeof setTimeout> | undefined;
  try {
    await session.bindExtensions({ onError(error) { observed.extensionError = error.error; } });
    session.setActiveToolsByName(['subagent']);
    observeSession(session, observed, marker, config.key);
    deadline = setTimeout(() => { void session.abort(); }, PROBE_TIMEOUT_MS);
    await session.prompt(
      `Use subagent exactly once with agent ${READER_NAME}, async false, context fresh, artifacts false, `
        + `and task: ${READER_TASK} Wait for completion and return only the child line. `
        + 'Do not guess or perform any other work.',
      { expandPromptTemplates: false },
    );
    const finalMarkerMatched = session.getLastAssistantText()?.trim() === marker;
    reportObservations(observed, finalMarkerMatched);
    verifyProbe(observed, finalMarkerMatched);
    console.log(JSON.stringify({
      status: 'passed', runtime: 'TypeScript Pi SDK', base_url: config.baseUrl,
      requested_model: config.model, response_models: [...observed.responseModels], api: config.api,
      model_requests: observed.modelRequests, http_responses: observed.responses,
      subagents_started: observed.started, subagents_completed: observed.completed,
      marker_matched: true, production_gateway_tested: false,
    }));
  } finally {
    clearTimeout(deadline);
    await closeSession(session);
  }
}

function restoreEnvironment(name: string, value: string | undefined): void {
  if (value === undefined) {
    delete process.env[name];
  } else {
    process.env[name] = value;
  }
}

async function smoke(config: ProbeConfiguration): Promise<void> {
  const home = await mkdtemp(join(tmpdir(), 'kernelcat-pi-sdk-smoke-'));
  const previousHome = process.env.HOME;
  const previousAgentDir = process.env.PI_CODING_AGENT_DIR;
  const previousDirectory = process.cwd();
  process.env.HOME = home;
  process.env.PI_CODING_AGENT_DIR = join(home, 'agent');
  process.chdir(home);
  try {
    await runProbe(config, home);
  } finally {
    process.chdir(previousDirectory);
    restoreEnvironment('HOME', previousHome);
    restoreEnvironment('PI_CODING_AGENT_DIR', previousAgentDir);
    await rm(home, { recursive: true, force: true });
  }
}

try {
  const config = configuration();
  if (config.discover) {
    await discover(config);
  } else {
    await smoke(config);
  }
} catch (error) {
  const message = error instanceof Error ? error.message : String(error);
  const cause = error instanceof Error && error.cause instanceof Error ? error.cause.message : undefined;
  console.error(JSON.stringify({
    status: 'failed', error: apiKey ? message.replaceAll(apiKey, '[redacted]') : message,
    cause: apiKey && cause ? cause.replaceAll(apiKey, '[redacted]') : cause,
    automatic_retry: false,
  }));
  process.exitCode = 1;
}
