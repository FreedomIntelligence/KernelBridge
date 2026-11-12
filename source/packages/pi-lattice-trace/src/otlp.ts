// OTLP/HTTP JSON export of a kernelbridge.trace/v1 trace, following the
// OpenTelemetry GenAI semantic conventions (status: Development) and adding
// OpenInference span kinds so Phoenix and Langfuse group spans the same way.
//
// Opt-in only: nothing is sent unless the user runs an export. Message content
// is not exported by default (OTel's NO_CONTENT default); prompts, tool
// arguments and outputs are separate switches. Gaps without a convention
// (compaction, file commits, lanes) use the kernelbridge.* namespace.
import { createHash } from 'node:crypto';
import type { Json, Step, Trace } from './trace-core.ts';

export const CONVENTIONS = 'otel-genai-semconv (Development, 2026-11) + openinference';
const VERSION = '0.2.0';
const INTERNAL = 1, CLIENT = 3, ERROR = 2;

export interface ContentCapture { prompts: boolean; tools: boolean; outputs: boolean }
export interface OtlpOptions { content?: Partial<ContentCapture>; service?: string }

/** OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true captures all; KERNELBRIDGE_OTEL_CAPTURE=prompts,tools,outputs picks. */
export function contentFromEnv(env: Record<string, string | undefined> = process.env): ContentCapture {
  const all = /^(true|1|span_only|span_and_event)$/i.test(env.OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT ?? env.OTEL_INSTRUMENTATION_GENAI_CAPTURE_CONTENT ?? '');
  const picked = new Set((env.KERNELBRIDGE_OTEL_CAPTURE ?? '').split(',').map(item => item.trim().toLowerCase()).filter(Boolean));
  return { prompts: all || picked.has('prompts'), tools: all || picked.has('tools'), outputs: all || picked.has('outputs') };
}

export function otlpEndpoint(env: Record<string, string | undefined> = process.env): string | null {
  if (env.OTEL_EXPORTER_OTLP_TRACES_ENDPOINT) return env.OTEL_EXPORTER_OTLP_TRACES_ENDPOINT;
  if (env.OTEL_EXPORTER_OTLP_ENDPOINT) return env.OTEL_EXPORTER_OTLP_ENDPOINT.replace(/\/+$/, '') + '/v1/traces';
  return null;
}

export function otlpHeaders(env: Record<string, string | undefined> = process.env): Record<string, string> {
  const raw = env.OTEL_EXPORTER_OTLP_TRACES_HEADERS ?? env.OTEL_EXPORTER_OTLP_HEADERS ?? '';
  const headers: Record<string, string> = {};
  for (const pair of raw.split(',')) {
    const at = pair.indexOf('='); if (at <= 0) continue;
    headers[decodeURIComponent(pair.slice(0, at).trim())] = decodeURIComponent(pair.slice(at + 1).trim());
  }
  return headers;
}

const hex = (value: string, length: number) => createHash('sha256').update(value).digest('hex').slice(0, length);
const traceIdOf = (session: string) => hex('kernelbridge.trace:' + session, 32);
/** A step's span key: its host action when it has one (known before the tool runs), else its step id. */
const spanKeyOf = (step: Step) => step.action_id ? 'action:' + step.action_id : String(step.id);

/** The W3C traceparent KernelBridge gives a command run for this action (kernelcat_executor commands.traceparent). */
export function traceparentFor(sessionId: string, actionId: string): string {
  const traceId = traceIdOf(sessionId);
  return `00-${traceId}-${hex(traceId + ':action:' + actionId, 16)}-01`;
}
type Attr = { key: string; value: Json };
function attr(key: string, value: unknown): Attr | null {
  if (value === null || value === undefined) return null;
  if (typeof value === 'boolean') return { key, value: { boolValue: value } };
  if (typeof value === 'number') return { key, value: Number.isInteger(value) ? { intValue: String(value) } : { doubleValue: value } };
  if (Array.isArray(value)) return { key, value: { arrayValue: { values: value.map(item => ({ stringValue: String(item) })) } } };
  return { key, value: { stringValue: typeof value === 'string' ? value : JSON.stringify(value) } };
}
const attrs = (entries: [string, unknown][]) => entries.map(([key, value]) => attr(key, value)).filter((item): item is Attr => item !== null);

export function traceToOtlp(trace: Trace, options: OtlpOptions = {}): Json {
  const content: ContentCapture = { prompts: false, tools: false, outputs: false, ...options.content };
  const conversation = trace.session.id ?? 'session';
  const traceId = traceIdOf(conversation);
  const spanId = (key: string) => hex(traceId + ':' + key, 16);
  const baseMs = trace.session.started_ns ? Math.round(trace.session.started_ns / 1e6) : Date.now();
  const nanos = (seconds: number) => (BigInt(baseMs) * 1_000_000n + BigInt(Math.round(seconds * 1e9))).toString();
  const steps = trace.steps;
  const end = steps.reduce((value, step) => Math.max(value, step.t1 ?? 0), 0);
  const spans: Json[] = [];
  const events = new Map<string, Json[]>();
  const span = (key: string, name: string, kind: number, parent: string | null, t0: number, t1: number, entries: [string, unknown][], failed?: string | null) => {
    spans.push({ traceId, spanId: spanId(key), ...(parent ? { parentSpanId: spanId(parent) } : {}), name, kind,
      startTimeUnixNano: nanos(t0), endTimeUnixNano: nanos(Math.max(t1, t0)), attributes: attrs(entries),
      events: events.get(key) ?? [], status: failed ? { code: ERROR, message: failed } : {} });
  };
  const event = (key: string, name: string, t: number, entries: [string, unknown][]) => {
    const list = events.get(key) ?? []; list.push({ name, timeUnixNano: nanos(t), attributes: attrs(entries) }); events.set(key, list);
  };
  const laneKey = (lane: string) => 'lane:' + lane;
  const spawnOf = new Map<string, Step>(); const returnOf = new Map<string, Step>();
  for (const step of steps) { if (step.kind === 'spawn' && step.child) spawnOf.set(step.child, step); if (step.kind === 'return' && step.child) returnOf.set(step.child, step); }
  const redacted = (enabled: boolean, value: unknown): [string, unknown][] => !enabled && value ? [['kernelbridge.content.redacted', true]] : [];
  const keyById = new Map(steps.map(step => [step.id, spanKeyOf(step)]));

  let compacted = new Set<string>();
  for (const step of steps) {
    const parentKey = step.turn && keyById.has(step.turn) ? keyById.get(step.turn)! : laneKey(step.agent);
    const common: [string, unknown][] = [['gen_ai.conversation.id', conversation], ['session.id', conversation],
      ['kernelbridge.step.id', step.id], ['kernelbridge.agent.id', step.agent], ['kernelbridge.context.tokens', step.ctx]];
    if (step.src === 'model') {
      const model = step.model ?? trace.session.model ?? 'unknown';
      const finish = step.status === 'error' ? 'error' : step.kind === 'final' ? 'stop' : 'tool_calls';
      span(spanKeyOf(step), `chat ${model}`, CLIENT, laneKey(step.agent), step.t0, step.t1, [...common,
        ['gen_ai.operation.name', 'chat'], ['gen_ai.request.model', model], ['gen_ai.response.finish_reasons', [finish]],
        ['gen_ai.usage.input_tokens', step.tokIn], ['gen_ai.usage.output_tokens', step.tokOut],
        ['gen_ai.usage.cache_read.input_tokens', step.cacheReal], ['gen_ai.usage.cache_write.input_tokens', step.cacheWrite],
        ['gen_ai.usage.reasoning.output_tokens', step.reasoningOut],
        ['gen_ai.conversation.compacted', compacted.has(step.agent) || null],
        ['openinference.span.kind', 'LLM'], ['llm.model_name', model],
        ['llm.token_count.prompt', step.tokIn], ['llm.token_count.completion', step.tokOut], ['llm.token_count.total', (step.tokIn ?? 0) + (step.tokOut ?? 0)],
        ['gen_ai.output.messages', content.outputs && step.thought ? JSON.stringify([{ role: 'assistant', parts: [{ type: 'text', content: step.thought }], finish_reason: finish }]) : null],
        ...redacted(content.outputs, step.thought)], step.status === 'error' ? step.title : null);
      compacted.delete(step.agent);
    } else if (step.src === 'tool' && step.kind !== 'return') {
      const finished = step.kind === 'spawn' && step.child ? returnOf.get(step.child) : undefined;
      span(spanKeyOf(step), `execute_tool ${step.tool}`, INTERNAL, parentKey, step.t0, finished ? finished.t1 : step.t1, [...common,
        ['gen_ai.operation.name', 'execute_tool'], ['gen_ai.tool.name', step.tool], ['gen_ai.tool.call.id', step.callId ?? null],
        ['kernelbridge.action.id', step.action_id ?? null], ['openinference.span.kind', 'TOOL'], ['tool.name', step.tool],
        ['gen_ai.tool.call.arguments', content.tools ? JSON.stringify(step.args ?? {}) : null],
        ['gen_ai.tool.call.result', content.outputs ? (finished ? finished.out : step.out) : null],
        ['kernelbridge.output.truncated', step.truncated ?? null], ['kernelbridge.output.original_length', step.originalLength ?? null],
        ['kernelbridge.file.commit', step.commit ?? null],
        ...redacted(content.tools, step.args && Object.keys(step.args).length), ...redacted(content.outputs, step.out)],
        ['error', 'denied'].includes(step.status) ? `${step.status}: ${step.title}` : null);
    } else if (step.kind === 'compact') {
      span(spanKeyOf(step), 'kernelbridge.compaction', INTERNAL, laneKey(step.agent), step.t0, step.t1, [...common,
        ['kernelbridge.compaction.tokens_before', step.before], ['kernelbridge.compaction.tokens_after', step.after],
        ['kernelbridge.compaction.summary', content.outputs ? step.note : null], ...redacted(content.outputs, step.note)]);
      compacted.add(step.agent);
    } else if (step.src === 'harness') {
      event(laneKey(step.agent), 'kernelbridge.' + step.kind, step.t0, [['kernelbridge.step.id', step.id], ['kernelbridge.title', content.prompts ? step.title : step.kind],
        ['gen_ai.input.messages', content.prompts && step.note ? JSON.stringify([{ role: 'user', parts: [{ type: 'text', content: step.note }] }]) : null],
        ...redacted(content.prompts, step.note)]);
    }
  }
  for (const lane of trace.rows) {
    const agent = trace.agents[lane] ?? {}; const laneSteps = steps.filter(step => step.agent === lane);
    const spawn = spawnOf.get(lane);
    const t0 = lane === trace.rows[0] ? 0 : Math.min(...laneSteps.map(step => step.t0), spawn?.t0 ?? Infinity);
    const t1 = lane === trace.rows[0] ? end : Math.max(...laneSteps.map(step => step.t1), returnOf.get(lane)?.t1 ?? 0);
    const name = lane === trace.rows[0] ? 'main' : agent.short ?? lane;
    span(laneKey(lane), `invoke_agent ${name}`, INTERNAL, lane === trace.rows[0] ? null : spawn ? spanKeyOf(spawn) : laneKey(trace.rows[0]),
      Number.isFinite(t0) ? t0 : 0, Number.isFinite(t1) ? t1 : end, [['gen_ai.operation.name', 'invoke_agent'], ['gen_ai.agent.name', name],
        ['gen_ai.conversation.id', conversation], ['session.id', conversation], ['openinference.span.kind', 'AGENT'],
        ['kernelbridge.agent.id', lane], ['kernelbridge.parent_agent.id', agent.parent ?? null]]);
  }
  return { resourceSpans: [{
    resource: { attributes: attrs([['service.name', options.service ?? (trace.labels?.harness === 'Pi' ? 'pi' : 'kernelbridge')],
      ['telemetry.sdk.name', 'pi-lattice-trace'], ['telemetry.sdk.version', VERSION], ['kernelbridge.trace.schema', trace.schema],
      ['kernelbridge.conventions', CONVENTIONS]]) },
    scopeSpans: [{ scope: { name: 'pi-lattice-trace', version: VERSION }, spans }],
  }] };
}

export interface ExportResult { endpoint: string; spans: number; status: number }

/** POSTs the trace as OTLP/HTTP JSON to OTEL_EXPORTER_OTLP_(TRACES_)ENDPOINT. */
export async function exportTrace(trace: Trace, { env = process.env, fetchImpl = fetch, service }: { env?: Record<string, string | undefined>; fetchImpl?: typeof fetch; service?: string } = {}): Promise<ExportResult> {
  const endpoint = otlpEndpoint(env);
  if (!endpoint) throw new Error('OTLP export needs OTEL_EXPORTER_OTLP_ENDPOINT (or OTEL_EXPORTER_OTLP_TRACES_ENDPOINT)');
  const body = traceToOtlp(trace, { content: contentFromEnv(env), service });
  const response = await fetchImpl(endpoint, { method: 'POST', headers: { 'Content-Type': 'application/json', ...otlpHeaders(env) }, body: JSON.stringify(body) });
  if (!response.ok) throw new Error(`OTLP export failed: HTTP ${response.status} ${await response.text().catch(() => '')}`.trim());
  return { endpoint, spans: body.resourceSpans[0].scopeSpans[0].spans.length, status: response.status };
}
