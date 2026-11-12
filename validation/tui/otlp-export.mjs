// OTLP export: OpenTelemetry GenAI span shapes, parent links, token semantics,
// stable ids, content off by default, and the HTTP/JSON POST with standard env.
import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { journal } from './trace-projection.mjs';
import { parseRows, projectTrace } from '../../source/apps/agent/src/trace/journal-trace.ts';
import { contentFromEnv, exportTrace, otlpEndpoint, otlpHeaders, traceToOtlp } from '../../source/packages/pi-lattice-trace/src/otlp.ts';

const trace = () => projectTrace(parseRows(journal()).rows, { now_ns: 1.8e18 + 9e10 });
const spansOf = body => body.resourceSpans[0].scopeSpans[0].spans;
const value = (span, key) => { const found = span.attributes.find(item => item.key === key); return found && Object.values(found.value)[0]; };

test('agent, model and tool spans nest like OpenTelemetry GenAI expects', () => {
  const spans = spansOf(traceToOtlp(trace()));
  const byId = new Map(spans.map(span => [span.spanId, span]));
  const root = spans.find(span => span.name === 'invoke_agent main');
  assert.ok(root && !root.parentSpanId, 'one root agent span');
  assert.equal(value(root, 'openinference.span.kind'), 'AGENT');
  const chat = spans.find(span => span.name === 'chat fixture-model');
  assert.equal(chat.kind, 3); assert.equal(byId.get(chat.parentSpanId), root);
  assert.equal(value(chat, 'gen_ai.operation.name'), 'chat');
  assert.equal(value(chat, 'gen_ai.usage.input_tokens'), '3000', 'input tokens include cached tokens (prompt_tokens)');
  const read = spans.find(span => span.name === 'execute_tool read');
  assert.equal(byId.get(read.parentSpanId).name, 'chat fixture-model', 'a tool call hangs under the model call that made it');
  assert.equal(value(read, 'openinference.span.kind'), 'TOOL');
  const child = spans.find(span => span.name === 'invoke_agent kernel-scout');
  assert.equal(byId.get(child.parentSpanId).name, 'execute_tool delegate', 'a subagent hangs under its delegate call');
  assert.equal(value(child, 'kernelbridge.parent_agent.id'), 'M/main');
  const compaction = spans.find(span => span.name === 'kernelbridge.compaction');
  assert.ok(Number(value(compaction, 'kernelbridge.compaction.tokens_before')) > 0);
  for (const span of spans) {
    assert.match(span.traceId, /^[0-9a-f]{32}$/); assert.match(span.spanId, /^[0-9a-f]{16}$/);
    assert.ok(BigInt(span.endTimeUnixNano) >= BigInt(span.startTimeUnixNano), span.name);
  }
  assert.equal(new Set(spans.map(span => span.traceId)).size, 1);
  assert.deepEqual(spansOf(traceToOtlp(trace())).map(span => span.spanId), spans.map(span => span.spanId), 'ids are derived, so re-export is stable');
});

test('no content leaves unless selected, and redaction is explicit', () => {
  const quiet = spansOf(traceToOtlp(trace()));
  const read = quiet.find(span => span.name === 'execute_tool read');
  assert.equal(value(read, 'gen_ai.tool.call.arguments'), undefined);
  assert.equal(value(read, 'gen_ai.tool.call.result'), undefined);
  assert.equal(value(read, 'kernelbridge.content.redacted'), true);
  assert.ok(!JSON.stringify(quiet).includes('Read solution.py and fix x'), 'the user prompt is not exported');
  const loud = spansOf(traceToOtlp(trace(), { content: { prompts: true, tools: true, outputs: true } }));
  const loudRead = loud.find(span => span.name === 'execute_tool read');
  assert.equal(value(loudRead, 'gen_ai.tool.call.arguments'), '{"path":"solution.py"}');
  assert.match(value(loudRead, 'gen_ai.tool.call.result'), /x = 1/);
  assert.ok(JSON.stringify(loud).includes('Read solution.py and fix x'));
  assert.deepEqual(contentFromEnv({ OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT: 'true' }), { prompts: true, tools: true, outputs: true });
  assert.deepEqual(contentFromEnv({ KERNELBRIDGE_OTEL_CAPTURE: 'tools' }), { prompts: false, tools: true, outputs: false });
  assert.deepEqual(contentFromEnv({}), { prompts: false, tools: false, outputs: false });
});

test('failed and denied steps carry an error status', () => {
  const sample = trace();
  sample.steps.find(step => step.tool === 'read').status = 'denied';
  const read = spansOf(traceToOtlp(sample)).find(span => span.name === 'execute_tool read');
  assert.equal(read.status.code, 2); assert.match(read.status.message, /^denied/);
});

test('standard OTLP environment variables pick the endpoint and headers', () => {
  assert.equal(otlpEndpoint({}), null);
  assert.equal(otlpEndpoint({ OTEL_EXPORTER_OTLP_ENDPOINT: 'http://collector:4318/' }), 'http://collector:4318/v1/traces');
  assert.equal(otlpEndpoint({ OTEL_EXPORTER_OTLP_ENDPOINT: 'http://a', OTEL_EXPORTER_OTLP_TRACES_ENDPOINT: 'http://b/custom' }), 'http://b/custom');
  assert.deepEqual(otlpHeaders({ OTEL_EXPORTER_OTLP_HEADERS: 'Authorization=Bearer%20abc, x-team = kb ' }), { Authorization: 'Bearer abc', 'x-team': 'kb' });
});

test('export POSTs OTLP/HTTP JSON and reports failures', async () => {
  const received = [];
  const server = createServer((request, response) => {
    let body = ''; request.on('data', chunk => { body += chunk; });
    request.on('end', () => { received.push({ path: request.url, type: request.headers['content-type'], auth: request.headers.authorization, body: JSON.parse(body) });
      response.writeHead(received.length === 1 ? 200 : 503); response.end(received.length === 1 ? '{}' : 'busy'); });
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  try {
    const env = { OTEL_EXPORTER_OTLP_ENDPOINT: `http://127.0.0.1:${server.address().port}`, OTEL_EXPORTER_OTLP_HEADERS: 'Authorization=Bearer%20t' };
    const result = await exportTrace(trace(), { env, service: 'kernelbridge' });
    assert.equal(received[0].path, '/v1/traces'); assert.equal(received[0].type, 'application/json'); assert.equal(received[0].auth, 'Bearer t');
    assert.equal(result.spans, spansOf(received[0].body).length);
    assert.equal(received[0].body.resourceSpans[0].resource.attributes.find(item => item.key === 'service.name').value.stringValue, 'kernelbridge');
    await assert.rejects(exportTrace(trace(), { env }), /HTTP 503/);
    await assert.rejects(exportTrace(trace(), { env: {} }), /OTEL_EXPORTER_OTLP_ENDPOINT/);
  } finally { await new Promise(resolve => server.close(resolve)); }
});
