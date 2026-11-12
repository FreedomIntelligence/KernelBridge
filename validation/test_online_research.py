"""Provider protocol fixtures plus real Gateway/PinnedCommand/Pi child admission."""
import base64
import hashlib
import http.client
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
import test_runtime as support
from test_subagents import Child
from kernelcat_executor.capabilities.delegation import SubagentEffect
from kernelcat_executor.contracts.models import Action
from kernelcat_worker.business_tools import configured_tools, PinnedCommand
from kernelcat_worker.configuration import BusinessToolConfig, SubagentConfig


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, support.ROOT / 'examples/online_research' / filename)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


prepare = module('online_prepare', 'prepare.py').prepare
driver = module('online_driver', 'driver.py')
VerifiedHTTPS = driver.PinnedHTTPS


class SocketFixture:
    def __init__(self, wire): self.wire = wire
    def makefile(self, *args): return io.BytesIO(self.wire)


def response(body=b'', status=200, headers=None):
    selected = {'Content-Type': 'text/html; charset=utf-8', 'Content-Length': str(len(body)), **(headers or {})}
    wire = f'HTTP/1.1 {status} Fixture\r\n'.encode() + b''.join(f'{k}: {v}\r\n'.encode() for k, v in selected.items()) + b'\r\n' + body
    result = http.client.HTTPResponse(SocketFixture(wire))
    result.begin()
    return result


class ConnectionFixture:
    responses = []
    requests = []
    def __init__(self, host, address, timeout, ca_bundle, http_proxy=None): self.host = host; self.address = address
    def request(self, method, target, headers): self.requests.append((self.host, self.address, method, target, headers))
    def getresponse(self): return self.responses.pop(0)
    def close(self): pass


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='kb-online-protocol-')
        self.base = Path(self.temp.name).resolve()
        self.prepared = prepare(True, self.base / 'prepared', sys.executable)
        self.config = self.prepared['online_research']
        ConnectionFixture.responses = []
        ConnectionFixture.requests = []
        self.patches = [patch.object(driver, 'PinnedHTTPS', ConnectionFixture),
                        patch.object(driver.socket, 'getaddrinfo', return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('1.1.1.1', 443))])]
        for item in self.patches: item.start()

    def tearDown(self):
        for item in reversed(self.patches): item.stop()
        self.temp.cleanup()

    def invoke(self, tool='online_fetch', arguments=None, identity='fixture'):
        return driver.main(self.config, {'id': identity, 'tool': tool, 'arguments': arguments or {'url': 'https://docs.python.org/3/#library'}})

    def test_search_receipts_and_lossless_text_pages(self):
        body = '<a class="result__a" href="https://docs.python.org/3/#library">Python 资料</a><a class="result__snippet">官方语义</a>'.encode()
        ConnectionFixture.responses = [response(body)]
        result = self.invoke('online_search', {'query': 'Python official documentation'})
        self.assertEqual(result['status'], 'completed', result)
        self.assertEqual(result['data']['results'][0]['url'], 'https://docs.python.org/3/#library')
        run = result['data']['run']
        manifest = driver.read({'run': run}, Path(self.config['state']))['data']
        self.assertEqual(manifest['driver_sha256'], self.config['driver_sha256'])
        rebuilt = b''
        offset = 0
        while True:
            page = driver.read({'run': run, 'path': 'text.txt', 'offset': offset, 'limit': 7}, Path(self.config['state']))['data']
            rebuilt += base64.b64decode(page['base64'])
            offset = page['next_offset']
            if page['eof']: break
        self.assertEqual(hashlib.sha256(rebuilt).hexdigest(), manifest['files']['text.txt']['sha256'])
        self.assertIn('官方语义', rebuilt.decode())
        raw = driver.read({'run': run, 'path': 'response.bin'}, Path(self.config['state']))['data']
        self.assertEqual(base64.b64decode(raw['base64']), body)
        self.assertNotIn('#', ConnectionFixture.requests[0][3])

    def test_fetch_bounds_and_complete_response_hash(self):
        self.config['max_text_chars'] = 256
        body = ('<title>Reference</title><script>ignore()</script><p>' + '中' * 300 + '</p>').encode()
        ConnectionFixture.responses = [response(body)]
        result = self.invoke()
        self.assertEqual(result['status'], 'completed', result)
        self.assertEqual(len(result['data']['text']), 256)
        self.assertTrue(result['data']['text_truncated'])
        self.assertNotIn('ignore()', result['data']['text'])
        self.assertEqual(result['data']['response']['full_response_sha256'], hashlib.sha256(body).hexdigest())
        self.assertEqual(ConnectionFixture.requests[0][3], '/3/')

    def test_short_content_length_is_unavailable_prefix(self):
        ConnectionFixture.responses = [response(b'short', headers={'Content-Length': '500'})]
        result = self.invoke()
        self.assertEqual(result['status'], 'failed')
        self.assertIn('incomplete_upstream', result['data']['reason'])
        metadata = json.loads((Path(self.config['state']) / result['data']['run'] / 'response.json').read_text())
        self.assertFalse(metadata['response_complete'])
        self.assertIsNone(metadata['full_response_sha256'])

    def test_byte_limit_retains_prefix_and_does_not_parse_it(self):
        self.config['max_response_bytes'] = 8
        ConnectionFixture.responses = [response(b'A' * 20)]
        result = self.invoke()
        self.assertEqual(result['status'], 'failed')
        folder = Path(self.config['state']) / result['data']['run']
        self.assertEqual((folder / 'response.bin').read_bytes(), b'A' * 8)
        self.assertFalse((folder / 'text.txt').exists())
        self.assertFalse(json.loads((folder / 'response.json').read_text())['response_complete'])

    def test_challenge_is_not_search_results(self):
        ConnectionFixture.responses = [response(b'<form id="challenge-form">verify you are human</form>')]
        result = self.invoke('online_search', {'query': 'public docs'})
        self.assertEqual(result['status'], 'failed')
        self.assertFalse(result['data']['available'])
        self.assertIn('challenge', result['data']['reason'])

    def test_public_dns_and_redirect_rejection(self):
        cases = ['https://127.0.0.1/', 'https://[::1]/', 'https://224.0.0.1/', 'https://[64:ff9b::a00:1]/',
                 'https://user:password@example.com/', 'https://example.com/?token=secret', 'http://example.com/', 'https://example.com:8443/']
        for url in cases:
            with self.subTest(url=url), self.assertRaises(driver.Unavailable): driver.public_url(url)
        with patch.object(driver.socket, 'getaddrinfo', return_value=[(2, 1, 6, '', ('10.1.1.1', 443))]):
            result = self.invoke(identity='private-dns')
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(ConnectionFixture.requests, [])
        ConnectionFixture.responses = [response(status=302, headers={'Location': 'https://127.0.0.1/private'})]
        result = self.invoke(identity='private-redirect')
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(len(ConnectionFixture.requests), 1)

    def test_search_never_follows_authenticated_redirect(self):
        self.config.update(provider='brave', endpoint='https://api.search.brave.com/res/v1/web/search', api_key_env='KB_TEST_SEARCH_ONLY')
        ConnectionFixture.responses = [response(status=302, headers={'Location': 'https://example.com/search'})]
        with patch.dict(os.environ, {'KB_TEST_SEARCH_ONLY': 'independent-key'}):
            result = self.invoke('online_search', {'query': 'public docs'})
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['data']['reason'], 'search_endpoint_redirected')
        self.assertEqual(len(ConnectionFixture.requests), 1)
        metadata = json.loads((Path(self.config['state']) / result['data']['run'] / 'response.json').read_text())
        self.assertEqual(metadata['hops'][0]['status'], 302)

    def test_preparation_pins_scope_and_rejects_inline_or_model_credentials(self):
        for profile in [{'provider': 'brave', 'api_key': 'inline'}, {'provider': 'brave', 'api_key_env': 'DEEPSEEK_API_KEY'},
                        {'provider': 'searxng', 'endpoint': 'https://127.0.0.1/search'}, {'mode': 'cached'}, {'http_proxy': 'http://user:key@127.0.0.1:7897'},
                        {'http_proxy': 'http://example.com:8080'}, {'http_proxy': 'http://127.0.0.1:7897/?key=secret'}]:
            with self.subTest(profile=profile), self.assertRaises(ValueError):
                prepare(profile, self.base / 'invalid', sys.executable)
        for row in self.prepared['business_tools']:
            self.assertEqual(row['env_allowlist'], [])
            for pin in row['files']:
                self.assertEqual(hashlib.sha256(Path(pin['path']).read_bytes()).hexdigest(), pin['sha256'])
        self.assertFalse(self.config['access']['workspace_reads'])
        self.assertFalse(self.config['access']['llm_calls'])

    def test_ca_bundle_is_from_selected_runtime_frozen_validated_and_pinned(self):
        tls = self.config['tls']
        frozen = Path(tls['ca_bundle'])
        self.assertEqual(tls['runtime'], sys.executable)
        self.assertGreater(tls['ca_count'], 0)
        connection = VerifiedHTTPS('docs.python.org', '1.1.1.1', 5, str(frozen))
        self.assertTrue(connection._context.check_hostname)
        self.assertEqual(connection._context.verify_mode, driver.ssl.CERT_REQUIRED)
        self.assertEqual(hashlib.sha256(frozen.read_bytes()).hexdigest(), tls['sha256'])
        self.assertTrue(all(any(pin['path'] == str(frozen) and pin['sha256'] == tls['sha256'] for pin in row['files'])
                            for row in self.prepared['business_tools']))
        selected = prepare({'ca_bundle': str(frozen)}, self.base / 'explicit-ca', sys.executable)
        self.assertEqual(selected['online_research']['tls']['discovery'], 'explicit')
        self.assertEqual(selected['online_research']['tls']['sha256'], tls['sha256'])
        invalid = self.base / 'invalid-ca.pem'; invalid.write_text('not a certificate')
        with self.assertRaisesRegex(ValueError, 'valid CA bundle'):
            prepare({'ca_bundle': str(invalid)}, self.base / 'rejected-ca', sys.executable)
        self.assertFalse((self.base / 'rejected-ca').exists())

    def test_explicit_proxy_tunnels_checked_ip_and_preserves_tls_hostname(self):
        from unittest.mock import MagicMock
        proxy = 'http://127.0.0.1:7897'
        connection = VerifiedHTTPS('html.duckduckgo.com', '1.1.1.1', 5, self.config['tls']['ca_bundle'], proxy)
        raw = MagicMock()
        raw.makefile.return_value = io.BytesIO(b'HTTP/1.1 200 Connection established\r\n\r\n')
        encrypted = MagicMock()
        with patch.object(driver.socket, 'create_connection', return_value=raw) as create, \
             patch.object(connection._context, 'wrap_socket', return_value=encrypted) as wrap:
            connection.connect()
        self.assertEqual(create.call_args.args[0], ('127.0.0.1', 7897))
        authority = b''.join(call.args[0] for call in raw.sendall.call_args_list)
        self.assertIn(b'CONNECT 1.1.1.1:443', authority)
        self.assertNotIn(b'html.duckduckgo.com', authority)
        wrap.assert_called_once_with(raw, server_hostname='html.duckduckgo.com')
        self.assertTrue(connection._context.check_hostname)
        self.assertEqual(connection._context.verify_mode, driver.ssl.CERT_REQUIRED)
        self.assertIs(connection.sock, encrypted)
        selected = prepare({'http_proxy': proxy}, self.base / 'proxy-config', sys.executable)
        self.assertEqual(selected['online_research']['transport']['http_proxy'], proxy)
        self.assertFalse(selected['online_research']['access']['proxy_environment'])

    def test_doh_raw_artifacts_and_bound_public_ip_are_used(self):
        host = 'docs.python.org'
        payload = {'Status': 0, 'TC': False, 'AD': False, 'Question': [{'name': host + '.', 'type': 1}],
                   'Answer': [{'name': host + '.', 'type': 5, 'TTL': 60, 'data': 'public.example.org.'},
                              {'name': 'public.example.org.', 'type': 1, 'TTL': 60, 'data': '1.1.1.1'}]}
        raw = json.dumps(payload).encode()
        self.config['transport'].update(dns_resolver='google_doh', http_proxy='http://127.0.0.1:7897',
                                       resolver_endpoint='https://dns.google/resolve', resolver_bootstrap_ip='8.8.8.8')
        ConnectionFixture.responses = [response(raw, headers={'Content-Type': 'application/dns-json'}),
                                       response(b'public source', headers={'Content-Type': 'text/plain'})]
        result = self.invoke()
        self.assertEqual(result['status'], 'completed', result)
        self.assertEqual(ConnectionFixture.requests[0][0:2], ('dns.google', '8.8.8.8'))
        self.assertEqual(ConnectionFixture.requests[1][0:2], (host, '1.1.1.1'))
        self.assertNotIn('X-Subscription-Token', ConnectionFixture.requests[0][4])
        self.assertEqual(result['data']['response']['hops'][0]['resolution']['retained_sha256'], hashlib.sha256(raw).hexdigest())
        read_tool = next(row for row in self.prepared['business_tools'] if row['name'] == 'online_read')
        self.assertIn('dns-0.bin', read_tool['parameters']['properties']['path']['enum'])
        page = driver.read({'run': result['data']['run'], 'path': 'dns-0.bin'}, Path(self.config['state']))['data']
        self.assertEqual(base64.b64decode(page['base64']), raw)

    def test_doh_rejects_unbound_private_and_cyclic_answers(self):
        base = {'Status': 0, 'TC': False, 'Question': [{'name': 'public.example.org.', 'type': 1}]}
        cases = [
            [{'name': 'public.example.org.', 'type': 1, 'TTL': 1, 'data': '127.0.0.1'}],
            [{'name': 'unrelated.example.org.', 'type': 1, 'TTL': 1, 'data': '1.1.1.1'}],
            [{'name': 'public.example.org.', 'type': 5, 'TTL': 1, 'data': 'public.example.org.'}],
        ]
        for answers in cases:
            with self.subTest(answers=answers), self.assertRaises(driver.Unavailable):
                driver.doh_addresses({**base, 'Answer': answers}, 'public.example.org')

    def test_bing_rss_has_actual_urls_and_rejects_html_or_entities(self):
        raw = b'<rss><channel><title>Public search</title><link>https://www.bing.com/</link><item><title>Python docs</title><link>https://docs.python.org/3/</link><description>Official docs</description></item></channel></rss>'
        config = {**self.config, 'provider': 'bing_rss'}
        rows, _ = driver.query_results(raw, {'content_type': 'text/xml'}, config)
        self.assertEqual(rows[0]['url'], 'https://docs.python.org/3/')
        for invalid in [b'<html>blocked</html>', b'<!DOCTYPE rss><rss><channel/></rss>', b'<rss><channel/></rss>']:
            with self.assertRaises(driver.Unavailable): driver.query_results(invalid, {'content_type': 'text/xml'}, config)
        selected = prepare({'provider': 'bing_rss', 'http_proxy': 'http://127.0.0.1:7897', 'dns_resolver': 'google_doh'}, self.base / 'bing-config', sys.executable)
        self.assertEqual(selected['online_research']['endpoint'], 'https://www.bing.com/search')
        self.assertEqual(selected['online_research']['provider_contract'], 'best_effort_rss_not_a_stable_api')
        self.assertEqual(selected['business_tools'][0]['env_allowlist'], [])

    def test_timeout_original_attempt_not_reexecuted(self):
        with patch.object(ConnectionFixture, 'getresponse', side_effect=TimeoutError):
            first = self.invoke(identity='timeout')
        self.assertEqual(first['status'], 'unknown')
        before = len(ConnectionFixture.requests)
        second = self.invoke(identity='timeout')
        self.assertEqual(second['status'], 'unknown')
        self.assertEqual(len(ConnectionFixture.requests), before)
        self.assertEqual(driver.read({'run': first['data']['run']}, Path(self.config['state']))['data']['status'], 'unknown')

    def test_brave_empty_and_malformed_schema(self):
        config = {**self.config, 'provider': 'brave'}
        for payload in [{'type': 'search', 'query': {'original': 'none'}},
                        {'type': 'search', 'query': {'original': 'none'}, 'web': None},
                        {'type': 'search', 'query': {'original': 'none'}, 'web': {'results': []}}]:
            self.assertEqual(driver.query_results(json.dumps(payload).encode(), {'content_type': 'application/json'}, config)[0], [])
        for payload in [{}, {'type': 'search', 'query': {'original': 'x'}, 'web': {}}, {'web': {'results': []}}]:
            with self.assertRaises(driver.Unavailable): driver.query_results(json.dumps(payload).encode(), {'content_type': 'application/json'}, config)

    def test_searxng_engine_failures_not_silently_empty(self):
        payload = {'results': [], 'unresponsive_engines': [['engine', 'CAPTCHA']]}
        with self.assertRaisesRegex(driver.Unavailable, 'engines_unavailable'):
            driver.query_results(json.dumps(payload).encode(), {'content_type': 'application/json'}, {**self.config, 'provider': 'searxng'})

    def test_brave_key_only_in_header_and_echoes_withheld(self):
        self.config.update(provider='brave', endpoint='https://api.search.brave.com/res/v1/web/search', api_key_env='KB_TEST_SEARCH_ONLY')
        key = 'independent-test-key'
        escaped = ''.join('\\u%04x' % ord(c) for c in key)
        ConnectionFixture.responses = [response(('"' + escaped + '"').encode(), headers={'Content-Type': 'application/json'})]
        with patch.dict(os.environ, {'KB_TEST_SEARCH_ONLY': key}):
            result = self.invoke('online_search', {'query': 'public document'})
        self.assertEqual(result['status'], 'failed')
        self.assertIn('credential', result['data']['reason'])
        self.assertEqual(ConnectionFixture.requests[0][4]['X-Subscription-Token'], key)
        self.assertNotIn(key, ConnectionFixture.requests[0][3])
        folder = Path(self.config['state']) / result['data']['run']
        self.assertFalse((folder / 'response.bin').exists())
        self.assertFalse(any(key in p.read_text() or escaped in p.read_text() for p in folder.iterdir()))

    def test_modified_artifact_is_not_read_as_original(self):
        ConnectionFixture.responses = [response(b'original', headers={'Content-Type': 'text/plain'})]
        result = self.invoke()
        (Path(self.config['state']) / result['data']['run'] / 'text.txt').write_text('tampered')
        with self.assertRaisesRegex(driver.Unavailable, 'hash_changed'):
            driver.read({'run': result['data']['run'], 'path': 'text.txt'}, Path(self.config['state']))


class OnlineGatewayTests(unittest.TestCase):
    setUp = support.RuntimeTests.setUp
    tearDown = support.RuntimeTests.tearDown
    session = support.RuntimeTests.session

    def register(self):
        prepared = prepare({'provider': 'brave', 'api_key_env': 'KB_ONLINE_MISSING_FIXTURE'}, self.base / 'online', sys.executable)
        specs = [BusinessToolConfig.model_validate_json(json.dumps(row)) for row in prepared['business_tools']]
        with patch.dict(os.environ, {}, clear=True):
            tools = configured_tools(specs, self.workspace, self.base / 'tool-state')
        self.assertTrue(all(isinstance(tool, PinnedCommand) for tool in tools))
        self.assertEqual({tool.spec.name for tool in tools}, {'online_search', 'online_fetch', 'online_read'})
        seed = tools[0].execute(Action(id='seed', task='test', tool='online_search', arguments={'query': 'public documentation'}))
        self.assertEqual(seed.status, 'failed', seed)
        self.assertEqual(seed.data['reason'], 'search_credential_unavailable')
        return tools, seed.data['run']

    def test_real_pi_gateway_calls_all_three_registered_tools(self):
        tools, run = self.register()
        session, provider, replay = self.session([('online_search', {'query': 'public documentation'}),
            ('online_fetch', {'url': 'https://127.0.0.1/'}), ('online_read', {'run': run})], extra_tools=tools)
        self.assertEqual(session.run(self.task)['status'], 'finished')
        self.assertTrue(all({'online_search', 'online_fetch', 'online_read'} <= names for names in provider.declarations))
        outcomes = [r['outcome'] for r in self.store.snapshot()['actions'].values() if r['action']['tool'].startswith('online_')]
        self.assertEqual([r['status'] for r in outcomes], ['failed', 'failed', 'completed'])
        self.assertTrue(all(r['data']['execution']['returncode'] == 0 for r in outcomes))
        replay._boundary()

    def test_readonly_child_can_use_admitted_online_tools(self):
        tools, run = self.register()
        delegate = SubagentEffect(SubagentConfig().model_dump(exclude={'enabled'}))
        session, _, replay = self.session([('delegate', {'agent': 'kernel-scout', 'task': 'Read online receipt', 'mode': 'read_only'})], extra_tools=[delegate, *tools])
        child = Child([('online_read', {'run': run})]); session.child_proposers = {'primary': child}
        self.assertEqual(session.run(self.task)['status'], 'finished')
        self.assertTrue(all({'online_search', 'online_fetch', 'online_read'} <= names for names in child.tools))
        leaf = [r for r in self.store.snapshot()['actions'].values() if '/leaf/' in r['action']['id'] and r['action']['tool'] == 'online_read']
        self.assertEqual(len(leaf), 1)
        self.assertEqual(leaf[0]['outcome']['status'], 'completed')
        replay._boundary()

    def test_readonly_child_sees_only_parent_admitted_online_subset(self):
        tools, run = self.register()
        delegate = SubagentEffect(SubagentConfig().model_dump(exclude={'enabled'}))
        session, _, replay = self.session([('delegate', {'agent': 'kernel-scout', 'task': 'Read retained evidence'})], extra_tools=[delegate, tools[2]])
        child = Child([('online_read', {'run': run})]); session.child_proposers = {'primary': child}
        self.assertEqual(session.run(self.task)['status'], 'finished')
        self.assertTrue(all('online_read' in names and not {'online_search', 'online_fetch'} & names for names in child.tools))
        replay._boundary()

    def test_analysis_child_cannot_invoke_online_tool(self):
        tools, run = self.register()
        delegate = SubagentEffect(SubagentConfig().model_dump(exclude={'enabled'}))
        session, _, replay = self.session([('delegate', {'agent': 'kernel-scout', 'task': 'Analyze without tools', 'mode': 'analysis'})], extra_tools=[delegate, *tools])
        child = Child([('online_read', {'run': run})]); session.child_proposers = {'primary': child}
        self.assertEqual(session.run(self.task)['status'], 'finished')
        self.assertEqual(child.tools, [set()])
        rows = list(self.store.snapshot()['actions'].values())
        self.assertFalse(any('/leaf/' in r['action']['id'] and r['action']['tool'] == 'online_read' for r in rows))
        result = next(r['outcome'] for r in rows if r['action']['tool'] == 'delegate')
        self.assertEqual(result['data']['reason'], 'unexpected_tool')
        replay._boundary()

    def test_changed_ca_pin_fails_before_any_network_command(self):
        tools, _ = self.register()
        ca = next(Path(pin.path) for pin in tools[1].pins if pin.path.endswith('ca-bundle.pem'))
        ca.chmod(0o600); ca.write_text('altered trust roots')
        result = tools[1].execute(Action(id='altered-ca', task='test', tool='online_fetch', arguments={'url': 'https://docs.python.org/3/'}))
        self.assertEqual(result.status, 'failed')
        self.assertEqual(result.data, {'reason': 'business_tool_input_changed', 'unexecuted': True})

    def test_changed_pin_fails_before_command_and_repeated_attempt_is_unknown(self):
        tools, _ = self.register()
        action = Action(id='duplicate', task='test', tool='online_search', arguments={'query': 'public docs'})
        first = tools[0].execute(action)
        self.assertEqual(first.status, 'failed')
        self.assertEqual(tools[0].execute(action).status, 'unknown')
        Path(tools[0].pins[0].path).write_text('changed driver')
        result = tools[0].execute(Action(id='changed', task='test', tool='online_search', arguments={'query': 'public docs'}))
        self.assertEqual(result.data, {'reason': 'business_tool_input_changed', 'unexecuted': True})


if __name__ == '__main__': unittest.main()
