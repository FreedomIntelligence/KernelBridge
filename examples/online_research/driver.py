"""Pinned, standard-library-only public GET research with original response receipts."""
import base64
from datetime import datetime, timezone
import hashlib
from html import unescape
from html.parser import HTMLParser
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import signal
import socket
import ssl
import sys
import time
from urllib.parse import parse_qs, unquote, urlencode, urljoin, urlsplit, urlunsplit


class Unavailable(Exception):
    pass


class Deadline(Exception):
    pass


def sha(data):
    return hashlib.sha256(data).hexdigest()


def save(path, data):
    data = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False, allow_nan=False, indent=2).encode()
    with path.open('xb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def public_address(value):
    address = ipaddress.ip_address(value)
    if not address.is_global or address.is_multicast or address.is_reserved:
        return False
    if address.version == 6:
        # Translation/tunnel addresses must not turn an apparently global IPv6
        # literal into a local IPv4 destination after our DNS check.
        if address.ipv4_mapped or address.sixtofour or address.teredo:
            return False
        if address in ipaddress.ip_network('64:ff9b::/96') or address in ipaddress.ip_network('64:ff9b:1::/48'):
            return False
    return True


def public_url(url):
    if not isinstance(url, str) or not url or len(url) > 4096 or re.search(r'[\x00-\x20\x7f\\]', url):
        raise Unavailable('invalid_public_url')
    try:
        parts = urlsplit(url)
        host = (parts.hostname or '').lower().rstrip('.')
        if (parts.scheme != 'https' or not host or parts.username is not None or parts.password is not None
                or parts.port not in (None, 443)):
            raise ValueError()
        host = host.encode('idna').decode('ascii')
    except ValueError:
        raise Unavailable('https_public_port_443_without_credentials_required') from None
    if ('.' not in host and ':' not in host) or host.endswith(('.localhost', '.local', '.internal', '.home', '.lan', '.test', '.invalid')):
        raise Unavailable('private_or_local_target')
    try:
        if not public_address(host):
            raise Unavailable('private_or_local_target')
    except ValueError:
        pass
    for name in {*parse_qs(parts.query, keep_blank_values=True), *parse_qs(parts.fragment, keep_blank_values=True)}:
        if re.search(r'(?:token|secret|password|passwd|credential|signature|authorization|api[-_]?key|access[-_]?key|^key$|^sig$|^auth$|^jwt$|session|bearer)', name, re.I):
            raise Unavailable('credential_bearing_url')
    if re.search(r'\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b', unquote(url)):
        raise Unavailable('credential_bearing_url')
    return parts, host


class PinnedHTTPS(http.client.HTTPSConnection):
    """TLS hostname verification with a socket to an already checked public IP."""
    def __init__(self, hostname, address, timeout):
        super().__init__(hostname, 443, timeout=timeout, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        sock = socket.create_connection((self.address, 443), self.timeout)
        try:
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            sock.close()
            raise


def get(url, config, output, *, credential=None, redirects=True):
    deadline = time.monotonic() + config['timeout_seconds']
    hops = []
    try:
        for _ in range(config['max_redirects'] + 1):
            parts, host = public_url(url)
            url = parts._replace(fragment='').geturl()
            addresses = list(dict.fromkeys(item[4][0] for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)))
            if not addresses or any(not public_address(ip) for ip in addresses):
                raise Unavailable('dns_resolved_private_or_nonpublic_target')
            remaining = deadline - time.monotonic()
            if remaining <= 0: raise Deadline()
            connection = PinnedHTTPS(host, addresses[0], remaining)
            headers = {'User-Agent': 'KernelBridge-OnlineResearch/1.0', 'Accept': 'text/html,application/json,text/plain;q=0.9,*/*;q=0.1', 'Accept-Encoding': 'identity'}
            if credential: headers['X-Subscription-Token'] = credential
            try:
                target = urlunsplit(('', '', parts.path or '/', parts.query, ''))
                connection.request('GET', target, headers=headers)
                response = connection.getresponse()
                hop = {'url': url, 'status': response.status, 'connected_ip': addresses[0],
                       'content_type': response.getheader('Content-Type', ''), 'content_encoding': response.getheader('Content-Encoding', 'identity')}
                if credential and credential in json.dumps(hop):
                    hops.append({'url': url, 'status': response.status, 'headers_withheld': True})
                    raise Unavailable('provider_echoed_credential_headers_withheld')
                hops.append(hop)
                if response.status in (301, 302, 303, 307, 308):
                    # Search credentials never follow redirects, even same-origin.
                    if not redirects: raise Unavailable('search_endpoint_redirected')
                    location = response.getheader('Location')
                    if not location: raise Unavailable('redirect_without_location')
                    next_url = urljoin(url, location)
                    public_url(next_url)
                    url = next_url
                    continue
                incomplete = False
                try:
                    raw = response.read(config['max_response_bytes'] + 1)
                except http.client.IncompleteRead as error:
                    raw, incomplete = error.partial, True
                # read(amt) does not necessarily raise on a short Content-Length
                # response. Never label a dropped-connection prefix as complete.
                incomplete = incomplete or response.length not in (None, 0)
                over_limit = len(raw) > config['max_response_bytes']
                raw = raw[:config['max_response_bytes']]
                # A provider echo of the configured credential is withheld, never archived.
                if credential and echoed_credential(raw, credential):
                    raise Unavailable('provider_echoed_credential_response_withheld')
                save(output / 'response.bin', raw)
                metadata = {'hops': hops, 'final_url': url, 'status': response.status,
                            'content_type': hop['content_type'], 'retained_bytes': len(raw),
                            'response_complete': not (over_limit or incomplete), 'retained_sha256': sha(raw),
                            'full_response_sha256': sha(raw) if not (over_limit or incomplete) else None}
                save(output / 'response.json', metadata)
                if over_limit: raise Unavailable('response_exceeds_byte_limit_retained_prefix_only')
                if incomplete: raise Unavailable('incomplete_upstream_response_retained_prefix_only')
                if response.status != 200: raise Unavailable('upstream_http_' + str(response.status))
                if hop['content_encoding'].lower() not in ('', 'identity'):
                    raise Unavailable('unsupported_content_encoding')
                return raw, metadata
            finally:
                connection.close()
        raise Unavailable('redirect_limit_exceeded')
    finally:
        if hops and not (output / 'response.json').exists():
            save(output / 'response.json', {'hops': hops, 'response_complete': False, 'full_response_sha256': None})

class TextHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.suppressed, self.title, self.in_title = [], [], [], False

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style', 'noscript', 'svg'):
            self.suppressed.append(tag)
        if tag == 'title': self.in_title = True
        if not self.suppressed and tag in ('p', 'br', 'div', 'li', 'h1', 'h2', 'h3', 'pre', 'tr'):
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if self.suppressed and self.suppressed[-1] == tag: self.suppressed.pop()
        if tag == 'title': self.in_title = False

    def handle_data(self, data):
        if self.in_title: self.title.append(data)
        if not self.suppressed: self.parts.append(data)

    def text(self):
        return '\n'.join(line.strip() for line in ''.join(self.parts).splitlines() if line.strip())


class DDGHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.results, self.capture, self.depth = [], None, 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = attrs.get('class', '').split()
        if tag == 'a' and 'result__a' in classes:
            href = attrs.get('href', '')
            if href.startswith('//'): href = 'https:' + href
            parsed = urlsplit(href)
            if parsed.hostname and parsed.hostname.endswith('duckduckgo.com'):
                href = parse_qs(parsed.query).get('uddg', [''])[0]
            self.results.append({'title': '', 'url': href, 'snippet': ''})
            self.capture, self.depth = 'title', 1
        elif 'result__snippet' in classes and self.results:
            self.capture, self.depth = 'snippet', 1
        elif self.capture and tag not in ('br', 'img', 'input', 'meta', 'link', 'hr'):
            self.depth += 1

    def handle_endtag(self, tag):
        if self.capture:
            self.depth -= 1
            if self.depth <= 0: self.capture = None

    def handle_data(self, data):
        if self.capture: self.results[-1][self.capture] += data


def decoded(raw, content_type):
    match = re.search(r'charset=["\']?([A-Za-z0-9._-]+)', content_type, re.I)
    try: return raw.decode(match.group(1) if match else 'utf-8', errors='replace')
    except LookupError: return raw.decode('utf-8', errors='replace')


def challenged(text):
    lowered = text.lower()
    return any(marker in lowered for marker in ('anomaly.js', 'challenge-form', 'cf-chl-', 'verify you are human',
        'unfortunately, bots use duckduckgo too', 'complete the security check', 'enable javascript and cookies to continue'))


def strip_markup(value):
    parser = TextHTML()
    parser.feed(str(value))
    return parser.text()


def echoed_credential(raw, credential):
    text = raw.decode('utf-8', errors='replace')
    if credential in unescape(text) or credential in strip_markup(text):
        return True
    try:
        return credential in json.dumps(json.loads(text), ensure_ascii=False)
    except (ValueError, TypeError):
        return False


def query_results(raw, metadata, config):
    body = decoded(raw, metadata['content_type'])
    if challenged(body): raise Unavailable('upstream_challenge_no_results')
    if config['provider'] == 'duckduckgo_html':
        parser = DDGHTML()
        parser.feed(body)
        if not parser.results and not re.search(r'(?:No results found|class=["\']no-results)', body, re.I):
            raise Unavailable('unrecognized_search_html_no_results')
        rows = parser.results
        warnings = []
    else:
        try:
            payload = json.loads(body)
            if config['provider'] == 'brave':
                # Official schema permits absent/null web, but requires results
                # when the web object exists. Do not turn arbitrary JSON into []:
                # the enclosing search response must identify its query/type.
                if (not isinstance(payload, dict) or payload.get('type') != 'search'
                        or not isinstance(payload.get('query'), dict)
                        or not isinstance(payload['query'].get('original'), str)):
                    raise ValueError()
                rows = [] if payload.get('web') is None else payload['web']['results']
            else:
                rows = payload['results']
            if not isinstance(rows, list): raise ValueError()
            warnings = payload.get('unresponsive_engines', []) if config['provider'] == 'searxng' else []
            if not rows and warnings: raise Unavailable('search_engines_unavailable_no_results')
        except (ValueError, KeyError, TypeError):
            raise Unavailable('invalid_search_provider_response') from None
    results = []
    for row in rows:
        if not isinstance(row, dict): raise Unavailable('invalid_search_result')
        url = row.get('url')
        try: public_url(url)
        except Unavailable: continue
        results.append({'citation': 'source-' + str(len(results) + 1), 'title': strip_markup(row.get('title', '')),
                        'url': url, 'snippet': strip_markup(row.get('snippet', row.get('description', row.get('content', ''))))})
    if rows and not results: raise Unavailable('no_safe_public_result_urls')
    return results, warnings


def run(action, config, output):
    args, tool = action['arguments'], action['tool']
    if tool == 'online_search':
        query = args['query']
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 512 or re.search(r'[\x00-\x1f]', query):
            raise Unavailable('invalid_explicit_query')
        count = args.get('count', config['max_results'])
        if type(count) is not int or not 1 <= count <= config['max_results']: raise Unavailable('invalid_result_count')
        key_name = config.get('api_key_env')
        credential = os.environ.get(key_name, '') if key_name else None
        if key_name and not credential: raise Unavailable('search_credential_unavailable')
        if credential and (credential in query or re.search(r'[\r\n]', credential)):
            raise Unavailable('credential_in_query_or_invalid_credential')
        if re.search(r'-----BEGIN .*PRIVATE KEY-----|\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,})\b', query):
            raise Unavailable('credential_like_query')
        parameters = {'q': query}
        if config['provider'] == 'brave': parameters['count'] = count
        if config['provider'] == 'searxng': parameters['format'] = 'json'
        raw, metadata = get(config['endpoint'] + '?' + urlencode(parameters), config, output, credential=credential, redirects=False)
        rows, warnings = query_results(raw, metadata, config)
        if credential and credential in json.dumps([rows, warnings], ensure_ascii=False):
            raise Unavailable('provider_echoed_credential_parsed_result_withheld')
        save(output / 'text.txt', json.dumps(rows, ensure_ascii=False, indent=2).encode())
        # Full parsed rows and response remain available through online_read.
        bounded = [{**row, 'title': row['title'][:512], 'snippet': row['snippet'][:1500]} for row in rows[:count]]
        return {'available': True, 'provider': config['provider'], 'provider_contract': config['provider_contract'],
                'results': bounded, 'retained_result_count': len(rows), 'results_truncated': len(rows) > count,
                'partial_provider_failure': bool(warnings), 'provider_warnings': str(warnings)[:2000] if warnings else None,
                'response': metadata}
    if tool != 'online_fetch': raise Unavailable('unknown_online_tool')
    raw, metadata = get(args['url'], config, output)
    content_type = metadata['content_type'].split(';', 1)[0].strip().lower()
    if not (content_type.startswith('text/') or content_type in ('application/json', 'application/xml', 'application/javascript', 'application/xhtml+xml')):
        raise Unavailable('unsupported_nontext_content_retained')
    text = decoded(raw, metadata['content_type'])
    if challenged(text): raise Unavailable('upstream_challenge_no_content')
    title = ''
    if content_type in ('text/html', 'application/xhtml+xml'):
        parser = TextHTML()
        parser.feed(text)
        text, title = parser.text(), ''.join(parser.title)
    save(output / 'text.txt', text.encode())
    maximum = config['max_text_chars']
    return {'available': True, 'url': metadata['final_url'], 'title': title[:512], 'text': text[:maximum],
            'text_characters': len(text), 'text_truncated': len(text) > maximum, 'response': metadata}


def read(args, root):
    identity = args['run']
    if not re.fullmatch('[a-f0-9]{64}', identity): raise Unavailable('invalid_receipt_id')
    folder = root / identity
    if folder.resolve() != folder: raise Unavailable('linked_receipt')
    manifest_path = folder / 'manifest.json'
    if not manifest_path.is_file():
        return {'status': 'unknown', 'data': {'run': identity, 'available': False, 'reason': 'receipt_missing_or_incomplete_do_not_retry_automatically'}}
    if manifest_path.is_symlink(): raise Unavailable('linked_receipt_manifest')
    manifest = json.loads(manifest_path.read_text())
    if 'path' not in args: return {'status': 'completed', 'data': manifest}
    name = args['path']
    if name not in manifest['files']: raise Unavailable('artifact_not_in_receipt')
    path = folder / name
    if path.resolve() != path or path.parent != folder: raise Unavailable('unsafe_artifact_path')
    raw = path.read_bytes()
    if sha(raw) != manifest['files'][name]['sha256']: raise Unavailable('artifact_hash_changed')
    offset, limit = args.get('offset', 0), args.get('limit', 8192)
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 16384:
        raise Unavailable('invalid_page_bounds')
    page = raw[offset:offset + limit]
    data = {'run': identity, 'path': name, 'sha256': sha(raw), 'offset': offset,
            'next_offset': offset + len(page), 'total_bytes': len(raw), 'eof': offset + len(page) >= len(raw)}
    # Byte offsets are unambiguous, including pages split across UTF-8 characters.
    data.update({'encoding': 'base64', 'base64': base64.b64encode(page).decode()} if name == 'response.bin'
                else {'encoding': 'utf-8', 'text': page.decode(errors='replace'), 'base64': base64.b64encode(page).decode()})
    return {'status': 'completed', 'data': data}


def main(config, action):
    root = Path(config['state'])
    if root.resolve() != root: raise Unavailable('linked_receipt_root')
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if action['tool'] == 'online_read': return read(action['arguments'], root)
    identity = sha(action['id'].encode())
    output = root / identity
    try: output.mkdir(mode=0o700)
    except FileExistsError:
        return {'status': 'unknown', 'data': {'run': identity, 'available': False, 'reason': 'existing_online_attempt_read_original_receipt'}}
    retrieved_at = datetime.now(timezone.utc).isoformat()
    binding = {'driver_sha256': config['driver_sha256'],
               'configuration_sha256': sha(json.dumps(config, sort_keys=True, separators=(',', ':')).encode())}
    request_arguments = action['arguments']
    credential = os.environ.get(config.get('api_key_env') or '', '')
    rejection = None
    if action['tool'] == 'online_fetch':
        try: public_url(request_arguments.get('url'))
        except Unavailable as error:
            rejection = str(error)
    if credential and credential in json.dumps(request_arguments, ensure_ascii=False):
        rejection = 'configured_credential_in_request'
    if action['tool'] == 'online_search' and re.search(r'-----BEGIN .*PRIVATE KEY-----|\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,})\b', str(request_arguments.get('query', ''))):
        rejection = 'credential_like_query'
    if rejection:
        request_arguments = {'withheld': rejection}
    save(output / 'request.json', {'tool': action['tool'], 'arguments': request_arguments, 'started_at': retrieved_at,
                                 'provider': config['provider'], 'endpoint': config['endpoint'], **binding})
    try:
        signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(Deadline()))
        signal.alarm(config['timeout_seconds'])
        if rejection: raise Unavailable(rejection)
        result = run(action, config, output)
        status = 'completed'
    except (Deadline, TimeoutError, socket.timeout):
        status, result = 'unknown', {'available': False, 'reason': 'request_timeout_inspect_original_receipt_before_retry'}
    except Unavailable as error:
        status, result = 'failed', {'available': False, 'reason': str(error)}
    except Exception as error:
        # Do not echo exception messages: they can contain HTTP headers or secrets.
        status, result = 'failed', {'available': False, 'reason': 'online_request_unavailable', 'error_type': type(error).__name__}
    finally:
        signal.alarm(0)
    result.update({'run': identity, 'retrieved_at': retrieved_at, 'untrusted_external_content': True})
    save(output / 'result.json', result)
    files = {p.name: {'sha256': sha(p.read_bytes()), 'bytes': p.stat().st_size} for p in output.iterdir() if p.is_file()}
    manifest = {'schema_version': 'kernelbridge.online-receipt/v1', 'run': identity, 'tool': action['tool'],
                'status': status, 'provider': config['provider'], 'endpoint': config['endpoint'],
                'retrieved_at': retrieved_at, 'configuration_hash_format': 'canonical_json_utf8', **binding, 'files': files}
    save(output / 'manifest.json', manifest)
    return {'status': status, 'data': {**result, 'artifacts': files,
                                     'receipt_sha256': sha((output / 'manifest.json').read_bytes())}}


if __name__ == '__main__':
    try:
        config = json.loads(Path(sys.argv[1]).read_text())
        action = json.load(sys.stdin)
        outcome = main(config, action)
    except Exception as error:
        outcome = {'status': 'unknown' if isinstance(error, OSError) else 'failed',
                   'data': {'available': False, 'reason': 'online_driver_input_or_receipt_unavailable', 'error_type': type(error).__name__}}
    print(json.dumps(outcome, ensure_ascii=False, allow_nan=False))
