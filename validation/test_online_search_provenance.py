"""Public search constraints/provenance through real local response parsing.

Only DNS/socket-facing transport boundaries are fixtures; no network is used.
"""
import hashlib
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit
import xml.etree.ElementTree as ET

import test_online_research as online


def rss(query, urls, *, link=None):
    root = ET.Element('rss', version='2.0'); channel = ET.SubElement(root, 'channel')
    ET.SubElement(channel, 'title').text = 'Search: ' + query
    ET.SubElement(channel, 'link').text = link or 'https://www.bing.com/search?' + urlencode({'q': query})
    for index, url in enumerate(urls):
        item = ET.SubElement(channel, 'item')
        ET.SubElement(item, 'title').text = 'Original title ' + str(index)
        ET.SubElement(item, 'link').text = url
        ET.SubElement(item, 'description').text = 'Original search snippet ' + str(index)
    return ET.tostring(root, encoding='utf-8')


class SearchProvenanceTests(unittest.TestCase):
    def setUp(self):
        online.ProtocolTests.setUp(self)
        self.config.update(provider='bing_rss', endpoint='https://www.bing.com/search',
                           provider_contract='best_effort_rss_not_a_stable_api')

    def tearDown(self):
        online.ProtocolTests.tearDown(self)

    invoke = online.ProtocolTests.invoke

    def search(self, arguments, body, *, identity='search', content_type='text/xml; charset=utf-8'):
        online.ConnectionFixture.responses = [online.response(body, headers={'Content-Type': content_type})]
        result = self.invoke('online_search', arguments, identity)
        self.assertEqual(result['status'], 'completed', result)
        return result['data']

    def read_file(self, result, name):
        return (Path(self.config['state']) / result['run'] / name).read_bytes()

    def test_default_preserves_query_any_domains_original_rows_and_response(self):
        query = 'exact_symbol 中文 + a&b "two words"'
        urls = ['https://docs.example.com/a', 'https://unrelated.org/b']
        body = rss(query, urls); result = self.search({'query': query}, body)
        self.assertEqual([row['url'] for row in result['results']], urls)
        self.assertEqual(parse_qs(urlsplit(online.ConnectionFixture.requests[0][3]).query)['q'], [query])
        provenance = result['search']; self.assertEqual(provenance['query'], query)
        self.assertEqual(provenance['effective_query'], query); self.assertEqual(provenance['domains'], [])
        self.assertTrue(provenance['provider_query_echo']['matches_requested'])
        self.assertEqual(provenance['response_sha256'], hashlib.sha256(body).hexdigest())
        self.assertEqual(result['domain_filtered_count'], 0)
        self.assertEqual(self.read_file(result, 'response.bin'), body)
        retained = json.loads(self.read_file(result, 'text.txt'))
        self.assertEqual([row['url'] for row in retained], urls)
        self.assertEqual(provenance['parsed_results_sha256'], hashlib.sha256(json.dumps(
            retained, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest())
        self.assertEqual(json.loads(self.read_file(result, 'search.json')), provenance)

    def test_idna_case_subdomains_and_false_suffixes_are_filtered_independently(self):
        urls = ['https://EXAMPLE.com/a', 'https://docs.example.com/a', 'https://example.com.evil.org/a',
                'https://notexample.com/a', 'https://evil-example.com/a', 'https://bücher.de/a',
                'https://docs.xn--bcher-kva.de/a', 'https://xn--bcher-kva.de.evil.org/a']
        effective = 'symbol (site:example.com OR site:xn--bcher-kva.de)'
        body = rss(effective, urls)
        result = self.search({'query': 'symbol', 'domains': ['ExAmPlE.COM.', 'BÜCHER.DE']}, body)
        self.assertEqual(result['search']['domains'], ['example.com', 'xn--bcher-kva.de'])
        self.assertEqual(result['search']['effective_query'], effective)
        self.assertEqual([row['url'] for row in result['results']], [urls[i] for i in (0, 1, 5, 6)])
        self.assertEqual(result['domain_filtered_count'], 4); self.assertEqual(result['retained_result_count'], 8)
        self.assertEqual(len(json.loads(self.read_file(result, 'text.txt'))), 8)
        self.assertEqual(self.read_file(result, 'response.bin'), body)
        self.assertEqual(parse_qs(urlsplit(online.ConnectionFixture.requests[0][3]).query)['q'], [effective])

    def test_provider_ignoring_sites_returns_completed_empty_not_provider_failure(self):
        urls = ['https://unrelated.org/a', 'https://example.com.evil.org/a']
        result = self.search({'query': 'precise_symbol', 'domains': ['example.com']},
                             rss('precise_symbol site:example.com', urls))
        self.assertTrue(result['available']); self.assertEqual(result['results'], [])
        self.assertEqual(result['domain_filtered_count'], 2); self.assertEqual(result['domain_matched_count'], 0)
        self.assertFalse(result['partial_provider_failure']); self.assertIsNone(result['provider_warnings'])
        self.assertFalse(result['results_truncated']); self.assertEqual(result['search']['relevance_assessment'], 'not_performed')
        self.assertEqual(len(json.loads(self.read_file(result, 'text.txt'))), 2)
        self.assertEqual(len(online.ConnectionFixture.requests), 1)  # No fallback or automatic body fetch.

    def test_filter_applies_before_count_without_renumbering_original_sources(self):
        urls = ['https://unrelated.org/a', 'https://example.com/a', 'https://example.com/b', 'https://example.com/c']
        result = self.search({'query': 'symbol', 'domains': ['example.com'], 'count': 2},
                             rss('symbol site:example.com', urls))
        self.assertEqual([row['url'] for row in result['results']], urls[1:3])
        self.assertEqual([row['citation'] for row in result['results']], ['source-2', 'source-3'])
        self.assertTrue(result['results_truncated']); self.assertEqual(result['domain_matched_count'], 3)
        self.assertEqual(result['search']['returned_count'], 2); self.assertEqual(result['retained_result_count'], 4)

    def test_invalid_domain_shapes_paths_credentials_private_hosts_never_request_network(self):
        invalid = [[], None, 'example.com', {}, [False], [None], [''], ['https://example.com'],
                   ['example.com/path'], ['example.com?x=1'], ['user:password@example.com'], ['example.com:443'],
                   ['*.example.com'], ['example.com#fragment'], ['example.com\\path'], ['-bad.example.com'],
                   ['example..com'], ['127.0.0.1'], ['localhost'], ['private.local'], ['example.com\n'],
                   ['a' * 254], ['a.example.com'] * 9]
        for index, domains in enumerate(invalid):
            with self.subTest(domains=domains):
                result = self.invoke('online_search', {'query': 'public symbol', 'domains': domains}, identity='invalid-' + str(index))
                self.assertEqual(result['status'], 'failed', result)
                self.assertFalse(result['data']['available'])
                self.assertEqual(online.ConnectionFixture.requests, [])
        self.assertEqual(online.ConnectionFixture.requests, [])

    def test_domain_credential_token_is_withheld_before_receipt_or_request(self):
        token = 'sk-' + 'a' * 24
        result = self.invoke('online_search', {'query': 'public symbol', 'domains': [token + '.org']})
        self.assertEqual(result['status'], 'failed'); self.assertEqual(online.ConnectionFixture.requests, [])
        folder = Path(self.config['state']) / result['data']['run']
        for file in folder.iterdir(): self.assertNotIn(token.encode(), file.read_bytes())

    def test_public_result_url_safety_is_preserved_inside_allowed_domain(self):
        urls = ['https://user:password@example.com/a', 'https://example.com/a?token=secret',
                'http://example.com/a', 'https://example.com:8443/a', 'https://example.com/safe']
        result = self.search({'query': 'symbol', 'domains': ['example.com']}, rss('symbol site:example.com', urls))
        self.assertEqual([row['url'] for row in result['results']], [urls[-1]])
        self.assertEqual(result['retained_result_count'], 1)

    def test_query_echo_mismatch_or_absence_is_visible_without_relevance_guess(self):
        result = self.search({'query': 'expected'}, rss('different', ['https://unrelated.org/a']))
        self.assertFalse(result['search']['provider_query_echo']['matches_requested'])
        self.assertTrue(result['available']); self.assertFalse(result['partial_provider_failure'])
        self.assertEqual(result['search']['provider_query_echo']['query'], 'different')
        missing = self.search({'query': 'expected'}, rss('title only', [], link='https://www.bing.com/search'), identity='missing-echo')
        self.assertFalse(missing['search']['provider_query_echo']['declared'])
        self.assertIsNone(missing['search']['provider_query_echo']['matches_requested'])
        self.assertTrue(missing['available']); self.assertEqual(missing['results'], [])

    def test_json_provider_query_echo_and_html_provider_absence(self):
        self.config.update(provider='brave', endpoint='https://api.search.brave.com/res/v1/web/search', api_key_env='KB_TEST_SEARCH_ONLY')
        payload = {'type': 'search', 'query': {'original': 'symbol site:example.com'},
                   'web': {'results': [{'url': 'https://example.com/a', 'title': 'symbol', 'description': 'snippet'}]}}
        with patch.dict(os.environ, {'KB_TEST_SEARCH_ONLY': 'independent-key'}):
            result = self.search({'query': 'symbol', 'domains': ['example.com']}, json.dumps(payload).encode(), content_type='application/json')
        self.assertEqual(result['search']['provider_query_echo']['location'], 'json/query/original')
        self.assertTrue(result['search']['provider_query_echo']['matches_requested'])
        self.config.update(provider='searxng', endpoint='https://search.example.org/search', api_key_env=None)
        payload = {'query': 'symbol', 'results': []}
        result = self.search({'query': 'symbol'}, json.dumps(payload).encode(), identity='searx', content_type='application/json')
        self.assertTrue(result['search']['provider_query_echo']['matches_requested'])
        self.config.update(provider='duckduckgo_html', endpoint='https://html.duckduckgo.com/html/')
        result = self.search({'query': 'symbol'}, b'<a class="result__a" href="https://example.com/a">symbol</a>',
                             identity='html', content_type='text/html')
        self.assertFalse(result['search']['provider_query_echo']['declared'])

    def test_search_does_not_fetch_bodies_and_explicit_fetch_has_distinct_content_kind(self):
        search = self.search({'query': 'symbol'}, rss('symbol', ['https://example.com/a']))
        self.assertEqual(search['content_kind'], 'search_snippets'); self.assertFalse(search['source_bodies_fetched'])
        self.assertEqual(search['results'][0]['content_kind'], 'search_snippet')
        self.assertEqual(len(online.ConnectionFixture.requests), 1)
        body = b'<title>Reference</title><p>Actual fetched source body</p>'
        online.ConnectionFixture.responses = [online.response(body)]
        fetched = self.invoke('online_fetch', {'url': 'https://example.com/a'}, 'fetch-body')
        self.assertEqual(fetched['status'], 'completed'); data = fetched['data']
        self.assertEqual(data['content_kind'], 'fetched_page_text'); self.assertTrue(data['source_body_fetched'])
        self.assertIn('Actual fetched source body', data['text']); self.assertNotIn('search', data)
        self.assertEqual(len(online.ConnectionFixture.requests), 2)

    def test_schema_and_retained_provenance_read_remain_hash_bound(self):
        search_tool = next(tool for tool in self.prepared['business_tools'] if tool['name'] == 'online_search')
        domains = search_tool['parameters']['properties']['domains']
        self.assertEqual(domains['type'], 'array'); self.assertEqual(domains['maxItems'], 8)
        self.assertEqual(search_tool['parameters']['required'], ['query'])
        read_tool = next(tool for tool in self.prepared['business_tools'] if tool['name'] == 'online_read')
        self.assertIn('search.json', read_tool['parameters']['properties']['path']['enum'])
        result = self.search({'query': 'symbol'}, rss('symbol', []))
        read = self.invoke('online_read', {'run': result['run'], 'path': 'search.json'})
        self.assertEqual(json.loads(read['data']['text']), result['search'])
        folder = Path(self.config['state']) / result['run']; before = (folder / 'search.json').read_bytes()
        (folder / 'search.json').write_bytes(before + b' ')
        with self.assertRaisesRegex(online.driver.Unavailable, 'artifact_hash_changed'):
            self.invoke('online_read', {'run': result['run'], 'path': 'search.json'})
        self.assertEqual(len(online.ConnectionFixture.requests), 1)


if __name__ == '__main__': unittest.main()
