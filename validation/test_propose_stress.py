"""32-lane real HTTP -> APIProposer -> Gateway -> fsync Journal pressure checks.

The provider is a local protocol fixture, not a paid model or device benchmark.
Set KB_STRESS_REPORT_DIR to retain each original Journal and public HTTP hashes.
"""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Barrier, Event, Lock, Thread
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
for path in [*ROOT.glob('source/packages/*/src'), ROOT / 'source/apps/worker/src', ROOT / 'validation']:
    sys.path.insert(0, str(path))

import httpx
import test_subagent_recovery as helpers
from kernelcat_executor.capabilities.llm import APIProposer
from kernelcat_executor.contracts.models import Action, BudgetExceeded, InDoubt, Outcome, Policy, Task
from kernelcat_executor.contracts.providers import WireOptions
from kernelcat_executor.operations.gateway import Gateway
from kernelcat_executor.operations.requests import propose
from kernelcat_executor.state.journal import Journal


class Provider(ThreadingHTTPServer):
    request_queue_size = 128
    daemon_threads = True

    def __init__(self):
        self.mutex = Lock(); self.release = Event(); self.overlap = Event()
        self.active = self.peak = self.target = 0
        self.requests = []
        super().__init__(('127.0.0.1', 0), Handler)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_): pass

    def do_POST(self):
        raw = self.rfile.read(int(self.headers['Content-Length']))
        server = self.server
        with server.mutex:
            server.active += 1; server.peak = max(server.peak, server.active)
            server.requests.append({'body_sha256': hashlib.sha256(raw).hexdigest(), 'started_ns': time.time_ns()})
            record = server.requests[-1]
            if server.active >= server.target:
                server.overlap.set(); server.release.set()
        try:
            if not server.release.wait(30):
                raise TimeoutError('fixture never reached the required concurrent HTTP occupancy')
            # Keep successive rounds overlapping without introducing random timing.
            time.sleep(.015)
            reply = {'choices': [{'message': {'role': 'assistant', 'content': 'Original fixture response.'},
                                  'finish_reason': 'stop'}], 'usage': {'prompt_tokens': 7, 'completion_tokens': 3, 'total_tokens': 10}}
            body = json.dumps(reply).encode()
            self.send_response(200); self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)
        finally:
            with server.mutex:
                record['ended_ns'] = time.time_ns(); server.active -= 1


class ProposeStressTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='kb-propose-stress-'); self.root = Path(self.temp.name).resolve()
        self.provider = Provider(); self.thread = Thread(target=self.provider.serve_forever, daemon=True); self.thread.start()
        self.client = httpx.Client(trust_env=False, limits=httpx.Limits(max_connections=64, max_keepalive_connections=64))
        self.api = APIProposer('openai', f'http://127.0.0.1:{self.provider.server_port}/v1', 'fixture', 'fixture-key',
            timeout=45, client=self.client, wire_options=WireOptions(native_turns=True, tool_choice='auto'))
        self.store = Journal(self.root / 'events.jsonl'); self.metrics = {}; self.started = time.monotonic(); self.support = None

    def tearDown(self):
        self.provider.release.set(); self.provider.shutdown(); self.provider.server_close(); self.thread.join(5); self.client.close()
        destination = os.environ.get('KB_STRESS_REPORT_DIR')
        if destination:
            output = Path(destination) / self._testMethodName; output.mkdir(parents=True, exist_ok=False)
            if self.store.path.exists(): shutil.copyfile(self.store.path, output / 'events.jsonl')
            (output / 'http-receipts.json').write_text(json.dumps(self.provider.requests, indent=2) + '\n')
            (output / 'summary.json').write_text(json.dumps({**self.metrics, 'scope': 'local_real_http_protocol_fixture',
                'http_calls': len(self.provider.requests), 'peak_http_in_flight': self.provider.peak,
                'elapsed_seconds': time.monotonic() - self.started}, indent=2) + '\n')
        if self.support: self.support.tearDown()
        self.temp.cleanup()

    def registered(self, count, limit):
        self.store.initialize(limit, Policy())
        tasks = [Task(id=f'parent/delegate/{index}/leaf', scope='pressure', goal='Return a bounded fixture response') for index in range(count)]
        for task in tasks: self.store.register_task(task.model_dump(mode='json'))
        return tasks

    def request(self, gateway, task, identity, api=None):
        return propose(gateway, api or self.api, identity, task.id, 'primary',
                       {'policy': gateway.store.policy().model_dump(mode='json'), 'stress_id': identity}, (), ())

    def ledger(self, limit, *, expected_unknown=0):
        events = list(self.store._events(self.store.events()))
        held = {}; spent = 0; admits = Counter(); starts = Counter(); settles = Counter()
        peak_reserved = 0
        for event in events:
            if event['kind'] == 'admitted':
                identity = event['action']['id']; admits[identity] += 1
                self.assertEqual(admits[identity], 1); held[identity] = event['credits']
            elif event['kind'] == 'started':
                starts[event['action_id']] += 1; self.assertEqual(starts[event['action_id']], 1)
            elif event['kind'] == 'settled':
                identity = event['action_id']; settles[identity] += 1
                self.assertEqual(settles[identity], 1); spent += held.pop(identity)
            # Unknown effects deliberately retain their original reservation.
            reserved = sum(held.values()); peak_reserved = max(peak_reserved, reserved)
            self.assertGreaterEqual(reserved, 0)
            self.assertLessEqual(spent + reserved, limit)
        state = self.store.snapshot()
        self.assertEqual((state['spent'], state['reserved']), (spent, sum(held.values())))
        self.assertEqual(sum(item.get('outcome', {}).get('status') == 'unknown' for item in state['actions'].values()), expected_unknown)
        model_events = [row for row in events if row['kind'] == 'admitted' and row['action']['tool'] == '__propose__']
        wire_hashes = Counter(hashlib.sha256(row['action']['arguments']['request']['body_json'].encode()).hexdigest() for row in model_events)
        self.assertEqual(wire_hashes, Counter(row['body_sha256'] for row in self.provider.requests))
        self.metrics.update(journal_events=len(events), admitted_models=len(model_events), spent=spent,
                            reserved=state['reserved'], peak_reserved=peak_reserved, prefix_invariants_checked=True)
        return state

    def test_32_gateways_320_proposals_share_atomic_credit_limit(self):
        tasks = self.registered(32, 192); barrier = Barrier(32); self.provider.target = 16
        def lane(index):
            # Separate Journal instances additionally exercise the actual OS file lock.
            gateway = Gateway(Journal(self.store.path), [], 'pressure'); barrier.wait(30)
            completed = denied = 0
            for step in range(10):
                try:
                    outcome = self.request(gateway, tasks[index], f'{tasks[index].id}/model/{step}')
                    self.assertEqual(outcome.status, 'completed'); completed += 1
                except BudgetExceeded:
                    denied += 1
            return completed, denied
        with ThreadPoolExecutor(max_workers=32) as pool: results = list(pool.map(lane, range(32)))
        self.assertEqual(tuple(map(sum, zip(*results))), (192, 128))
        self.assertGreaterEqual(self.provider.peak, 16)
        state = self.ledger(192); self.assertEqual((state['spent'], state['reserved']), (192, 0))
        self.metrics.update(lanes=32, attempted_proposals=320, budget_rejections=128)

    def test_64_duplicate_calls_dispatch_once_and_replay_original_receipt(self):
        task = self.registered(1, 8)[0]; self.provider.target = 1; barrier = Barrier(64)
        identity = task.id + '/shared-original'
        def duplicate(_):
            gateway = Gateway(Journal(self.store.path), [], 'pressure'); barrier.wait(30)
            try: return self.request(gateway, task, identity).status
            except InDoubt: return 'fenced'
        with ThreadPoolExecutor(max_workers=64) as pool: replies = list(pool.map(duplicate, range(64)))
        self.assertTrue(set(replies) <= {'completed', 'fenced'}); self.assertIn('completed', replies)
        self.assertEqual(len(self.provider.requests), 1)
        replay = self.request(Gateway(Journal(self.store.path), [], 'pressure'), task, identity)
        self.assertEqual(replay.status, 'completed'); self.assertEqual(len(self.provider.requests), 1)
        state = self.ledger(8); self.assertEqual((state['spent'], state['reserved']), (1, 0))
        self.metrics.update(concurrent_duplicate_calls=64, duplicate_results=dict(Counter(replies)))

    def test_32_actual_child_permits_preserve_unknown_fence_and_original_recovery(self):
        self.support = helpers.RecoveryTests(methodName='runTest'); self.support.setUp()
        owner, rpc = self.support.authority(limit=32); self.store = self.support.store
        parent = Action(id='test/original-parent', task='test', tool='__fixture_parent__')
        owner.gateway.internal(parent, 0, lambda: Outcome())
        rpc.proposal_id = parent.id
        children = [self.support.begin(rpc, identity=str(index)) for index in range(32)]
        target = rpc.background[children[0][0].id].effects.task.id
        original = {}; api = self.api
        class LostAfterResponse:
            def prepare(self, context, tools): return api.prepare(context, tools)
            def invoke(self, request):
                reply = api.invoke(request)
                if target in request.body_json:
                    original['reply'] = reply
                    raise RuntimeError('injected controller loss after original HTTP response')
                return reply
        owner.child_proposers = {'primary': LostAfterResponse()}
        barrier = Barrier(32); self.provider.target = 16
        def child_model(entry):
            action, _ = entry; child = rpc.background[action.id].effects; barrier.wait(30)
            try: return action.id, child.model('primary')
            except InDoubt: return action.id, None
        with ThreadPoolExecutor(max_workers=32) as pool: outcomes = list(pool.map(child_model, children))
        self.assertEqual(sum(result is None for _, result in outcomes), 1)
        for identity, result in outcomes:
            if result is not None: rpc.plugin_settle({'permit': identity, 'status': 'completed', 'text': result['text']})
        unknown_id = children[0][0].id
        barrier = Barrier(32)
        def reject_settlement(_):
            barrier.wait(30)
            with self.assertRaises(InDoubt): rpc.plugin_settle({'permit': unknown_id, 'status': 'failed', 'text': ''})
        with ThreadPoolExecutor(max_workers=32) as pool: list(pool.map(reject_settlement, range(32)))
        rpc.close()
        self.assertIn(unknown_id, rpc.background)
        self.assertGreaterEqual(self.provider.peak, 16)
        state = self.ledger(300, expected_unknown=1); self.assertEqual(state['reserved'], 1)
        pending = next(row for row in state['actions'].values() if row.get('outcome', {}).get('status') == 'unknown')
        self.assertEqual(pending['action']['task'], target)
        # Recover the retained original response, never send another provider request.
        reopened = Journal(self.store.path)
        reopened.settle(Action.model_validate_json(json.dumps(pending['action'])), Outcome(data=original['reply'].model_dump(mode='json')), 0)
        rpc.plugin_settle({'permit': unknown_id, 'status': 'cancelled', 'text': ''})
        self.assertEqual(len(self.provider.requests), 32)
        state = self.ledger(300); self.assertEqual(state['reserved'], 0)
        self.metrics.update(actual_child_permits=32, unknown_fence_rejections=32, recovered_original_without_redispatch=True)

    def test_observed_parent_context_survives_32_concurrent_child_admissions(self):
        self.support = helpers.RecoveryTests(methodName='runTest'); self.support.setUp()
        owner, rpc, context, child = self.support.prepared_parent(); self.store = self.support.store
        owner.proposers = {'primary': self.api}; self.provider.target = 16
        barrier = Barrier(32)
        def lane(index):
            barrier.wait(30)
            return self.request(Gateway(self.store, [], child.scope), child, f'{child.id}/pressure/{index}')
        with ThreadPoolExecutor(max_workers=32) as pool:
            futures = [pool.submit(lane, index) for index in range(32)]
            self.assertTrue(self.provider.overlap.wait(30))
            self.assertGreater(self.store.snapshot()['reserved'] + self.store.snapshot()['spent'], context['budget']['spent'])
            result = rpc.model({'route': 'primary', 'context': context})
            self.assertEqual(result['text'], 'Original fixture response.')
            self.assertTrue(all(future.result().status == 'completed' for future in futures))
        state = self.ledger(300); self.assertEqual((state['spent'], state['reserved']), (33, 0))
        self.metrics.update(concurrent_child_proposals=32, authenticated_parent_context_admitted=True)


if __name__ == '__main__': unittest.main()
