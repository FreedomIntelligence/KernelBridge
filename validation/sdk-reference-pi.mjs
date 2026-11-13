import test from 'node:test';
import assert from 'node:assert/strict';
import { execFile } from 'node:child_process';
import { readFile, mkdtemp, chmod, readdir, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { promisify } from 'node:util';

const root = fileURLToPath(new URL('../', import.meta.url));
const execute = promisify(execFile);
async function setting(name) {
  try { return (await readFile(join(root, '.development', name), 'utf8')).trim(); }
  catch (error) { if (error.code === 'ENOENT') return undefined; throw error; }
}
const python = process.env.KERNELBRIDGE_SDK_PI_PYTHON ?? await setting('python');
const pythonpath = process.env.KERNELBRIDGE_SDK_PI_PYTHONPATH ?? await setting('pythonpath');

async function writableDirectories(directory) {
  await chmod(directory, 0o700);
  for (const entry of await readdir(directory, { withFileTypes: true })) {
    if (entry.isDirectory()) await writableDirectories(join(directory, entry.name));
  }
}

for (const mode of ['parent', 'child']) {
  test(`real Pi SDK ${mode} retains search progress and SDK pages in the next actual provider request and replays without rescanning`, {
    skip: !python || !pythonpath ? 'Requires installed development Python runtime or KERNELBRIDGE_SDK_PI_PYTHON{,PATH}' : false,
    timeout: 180000,
  }, async t => {
    const configured = JSON.parse(await readFile(join(root, 'source/apps/agent/package.json'), 'utf8'));
    const installed = JSON.parse(await readFile(join(root, 'source/apps/agent/node_modules/@earendil-works/pi-coding-agent/package.json'), 'utf8'));
    assert.equal(installed.version, configured.dependencies['@earendil-works/pi-coding-agent']);
    const retained = process.env.KERNELBRIDGE_SDK_PI_OUTPUT;
    const directory = retained ?? await mkdtemp(join(tmpdir(), 'kb-sdk-pi-'));
    if (!retained) t.after(async () => { await writableDirectories(directory); await rm(directory, { recursive: true, force: true }); });
    const result = await execute(python, ['-B', join(root, 'validation/sdk_reference_pi_fixture.py'), '--mode', mode,
      '--output', join(directory, mode)], {
      cwd: root, timeout: 150000, maxBuffer: 1024 * 1024,
      env: { PATH: `${dirname(process.execPath)}:/usr/bin:/bin`, LANG: 'C.UTF-8',
        PYTHONDONTWRITEBYTECODE: '1', PYTHONPATH: pythonpath, KERNELCAT_NODE: process.execPath },
    });
    const report = JSON.parse(result.stdout);
    assert.equal(report.status, 'passed');
    assert.equal(report.mode, mode);
    assert.ok(report.source_bytes > 16384);
    assert.equal(report.utf8_exact_reassembly, true);
    assert.equal(report.gateway_replay_without_rescan, true);
    assert.equal(report.complete_session_replay_without_model_or_tool, true);
    assert.equal(report.sdk_search_actions, 2);
    assert.equal(report.search_progress_verified_in_actual_wire, true);
    assert.equal(report.search_report_truncated_with_visible_cursor, true);
    assert.deepEqual(report.search_cursor_continuation_match_counts, [50, 1]);
    assert.ok(report.wire_requests_verified > report.sdk_read_actions);
    assert.ok(mode === 'parent' ? report.visible_page_bytes[0] > 8192 : report.visible_page_bytes[0] < 8192);
    t.diagnostic(JSON.stringify(report));
  });
}
