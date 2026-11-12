"""Replay real Pi session files through the pi-lattice-trace projection.

Usage: pi_real_replay.py --output DIR [--limit N] [--render K] [SESSION.jsonl ...]
Without paths it uses the largest Pi session files that KernelBridge runs left
under ~/.kernelbridge/runs/*/state/pi-sessions/. Only counts are printed and
recorded: these are private conversations, so no content leaves this machine.

For every session: the projection must not raise, every step must have finite
times on a known lane, and there must be one model step per assistant message.
Where the run's Journal is next to it, each model step's input tokens are
compared with the provider-reported prompt_tokens of the matching model call.
The K largest sessions are also rendered in headless Chrome (all tabs).
"""
import argparse
from collections import Counter
import json
import math
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))


def discover(limit):
    files = [path for path in (Path.home() / '.kernelbridge/runs').glob('*/state/pi-sessions/*/sessions/*.jsonl')]
    return sorted(files, key=lambda path: path.stat().st_size, reverse=True)[:limit]


def project(paths):
    script = ("import { traceFile } from './source/packages/pi-lattice-trace/bin/lattice-trace.ts';"
              "const out = []; for (const path of JSON.parse(process.argv[1])) {"
              " try { const t = await traceFile(path); out.push({ path, steps: t.steps.map(s => ({ src: s.src, kind: s.kind, agent: s.agent, t0: s.t0, t1: s.t1, tokIn: s.tokIn ?? null, status: s.status })), rows: t.rows }); }"
              " catch (error) { out.push({ path, error: String(error && error.stack || error) }); } }"
              " process.stdout.write(JSON.stringify(out));")
    result = subprocess.run(['node', '--input-type=module', '-e', script, json.dumps([str(p) for p in paths])], cwd=ROOT, capture_output=True, text=True, timeout=600)
    if result.returncode: raise RuntimeError(result.stderr[-2000:])
    return json.loads(result.stdout)


def journal_prompts(session):
    """prompt_tokens of the run's main-task model calls, in order (None where not reported)."""
    state = session.parents[3]
    journal = state / 'events.jsonl'
    if not journal.exists(): return None
    admitted, prompts = {}, []
    for line in journal.open():
        row = json.loads(line)
        if row.get('kind') == 'admitted' and row['action']['tool'] == '__propose__':
            action = row['action']['id']
            if '/leaf' not in action and '/compact/' not in action and '/goal-review/' not in action: admitted[action] = len(admitted)
        elif row.get('kind') == 'settled' and row.get('action_id') in admitted:
            usage = (row.get('outcome') or {}).get('data', {}).get('usage') or {}
            prompts.append(usage.get('prompt_tokens'))
    return prompts


def main():
    parser = argparse.ArgumentParser(); parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--limit', type=int, default=20); parser.add_argument('--render', type=int, default=3); parser.add_argument('paths', nargs='*', type=Path)
    args = parser.parse_args(); output = args.output.resolve(); output.mkdir(parents=True, exist_ok=False)
    paths = args.paths or discover(args.limit)
    results, summary = project(paths), {'sessions': len(paths), 'errors': 0, 'steps': 0, 'model_steps': 0, 'compared': 0, 'matched': 0, 'invalid_steps': 0}
    for item in results:
        if 'error' in item: summary['errors'] += 1; continue
        steps, rows = item['steps'], set(item['rows'])
        summary['steps'] += len(steps)
        summary['invalid_steps'] += sum(1 for s in steps if not (math.isfinite(s['t0']) and math.isfinite(s['t1']) and s['t1'] >= s['t0'] and s['agent'] in rows))
        lines = [json.loads(line) for line in open(item['path'])]
        assistants = sum(1 for row in lines if row.get('type') == 'message' and (row.get('message') or {}).get('role') == 'assistant')
        models = [s for s in steps if s['src'] == 'model' and s['agent'] == 'M/main']
        summary['model_steps'] += len(models)
        assert len(models) <= assistants, (item['path'], len(models), assistants)
        prompts = journal_prompts(Path(item['path']))
        if prompts:
            if len(prompts) == len(models):
                pairs = [(step['tokIn'], prompt) for step, prompt in zip(models, prompts) if prompt is not None]
                summary['compared'] += len(pairs); summary['matched'] += sum(a == b for a, b in pairs)
            else:
                # The run holds several Pi sessions (/new, /fork): each step must be one of the run's calls.
                pool = Counter(prompt for prompt in prompts if prompt is not None)
                for step in models:
                    summary['compared'] += 1
                    if pool[step['tokIn']] > 0: pool[step['tokIn']] -= 1; summary['matched'] += 1
    rendered = []
    if args.render:
        from trace_branches_render import serve, TABS
        from playwright.sync_api import sync_playwright
        for path in paths[:args.render]:
            process, url = serve(['node', 'source/packages/pi-lattice-trace/bin/lattice-trace.ts', str(path), '--no-open'])
            errors = []
            try:
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(channel='chrome', headless=True)
                    page = browser.new_page(viewport={'width': 1440, 'height': 900})
                    page.on('pageerror', lambda error: errors.append(str(error)[:200]))
                    page.on('console', lambda message: errors.append(message.text[:200]) if message.type == 'error' and 'Failed to load resource' not in message.text else None)
                    page.goto(url); page.wait_for_function('typeof STEPS !== "undefined" && STEPS.length > 0', timeout=30000)
                    for tab in TABS: page.evaluate(f'setTab({json.dumps(tab)})'); page.wait_for_timeout(200)
                    browser.close()
            finally: process.kill()
            rendered.append({'steps': next((len(r['steps']) for r in results if r.get('path') == str(path)), None), 'errors': len(errors), 'first_error': errors[0] if errors else None})
    summary['rendered'] = rendered
    (output / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n'); print(json.dumps(summary))
    failures = summary['errors'] or summary['invalid_steps'] or any(r['errors'] for r in rendered) or (summary['compared'] and summary['matched'] != summary['compared'])
    return 1 if failures else 0


if __name__ == '__main__': sys.exit(main())
