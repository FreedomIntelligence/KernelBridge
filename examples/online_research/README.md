# Public online research tools

These are KernelBridge provider adapters, not ports of Codex or Claude hosted search. They make public HTTPS GET requests for an explicit query or URL. They do not forward chat history, automatically read workspace files, or call a model to summarize a page.

Set `online_research` in an ordinary codegen config, a CANN profile, or the input consumed by the operator campaign preparation helper:

```json
{"online_research": true}
```

`true` selects `duckduckgo_html` at `https://html.duckduckgo.com/html/`. This no-key HTML interface is best effort, not a stable general-search API. Challenges, changed markup, unavailable network access, and failed responses produce an explicit unavailable outcome. Configuration alone does not establish a successful live search.

An explicit no-key alternative is Bing's public RSS response, also a best-effort interface rather than a stable API. It may return broad or incomplete results; inspect the actual sources. No fallback from a failed provider happens automatically:

```json
{
  "online_research": {
    "provider": "bing_rss",
    "http_proxy": "http://127.0.0.1:7897",
    "dns_resolver": "google_doh"
  }
}
```

The proxy is optional unless `google_doh` is selected. `google_doh` explicitly uses `https://dns.google/resolve` via fixed public bootstrap `8.8.8.8`, verifies TLS for `dns.google`, sends only the destination hostname and an A-record query, validates the returned CNAME chain and every address, and connects to the resulting public IP while preserving the destination TLS hostname. It never asks the proxy to resolve arbitrary destination hostnames. Default resolution remains the system resolver. Each hop retains raw DNS bytes, hashes and metadata as `dns-N.bin`/`dns-N.json`, available through `online_read`. This addresses environments with an incorrect system DNS answer without weakening the public-target boundary. The protocol follows Google's [JSON DoH contract](https://developers.google.com/speed/public-dns/docs/doh/json).

For Brave Search, use an independent search-service credential:

```json
{
  "online_research": {
    "provider": "brave",
    "api_key_env": "BRAVE_SEARCH_API_KEY",
    "timeout_seconds": 20,
    "max_response_bytes": 2097152,
    "max_text_chars": 12000,
    "max_results": 8
  }
}
```

Set the named environment variable, or put its value under the same name in the existing user-owned `0600` `~/.kernelbridge/credentials.json` file. Never put a key in a profile, endpoint URL, query, or source tree. Host startup loads only explicitly configured credential references. Search cannot reuse configured model credential names or values, including DeepSeek credentials. Missing Brave credentials are unavailable; the driver does not silently switch providers.

SearXNG requires an explicitly chosen public HTTPS instance with JSON search enabled:

```json
{"online_research": {"provider": "searxng", "endpoint": "https://YOUR_PUBLIC_INSTANCE/search"}}
```

An instance's JSON access, coverage, rate limits and availability are its operator's responsibility. Local/private instances and authenticated URL parameters are outside this adapter's public access scope. Brave, Bing RSS and DuckDuckGo endpoints are fixed; SearXNG's explicit endpoint is frozen in the prepared config. No provider endpoint is chosen by model input.

`prepare(profile, output, python)` in `prepare.py` performs no network or model work. It discovers and validates a CA bundle under the selected Python runtime (system PEM file, then that runtime's certifi bundle), copies the exact bytes into the frozen inputs, and pins its SHA-256 alongside driver/config. Set `"ca_bundle": "/absolute/path/to/trusted-ca.pem"` inside `online_research` to choose an explicit trust bundle. No usable bundle is a preparation error. Driver TLS verification and hostname checks stay enabled; later CA changes require fresh preparation. No global Python certificate installation or TLS bypass is performed. It returns:

- `business_tools`: `online_search`, `online_fetch`, `online_read`, with driver/config SHA-256 pins and bounded argument schemas.
- `online_research`: frozen driver configuration for artifact/provenance use. Do not add this field to the strict worker runtime config.
- `resources`: the provider, endpoint, credential variable name, access scope, pins and usage descriptor for an admitted resource map.

Merge `business_tools` into existing tools; duplicate names are an error. Ordinary codegen and CANN preparation integrate this helper automatically when configured. CANN `run.mjs`, its `--config` compatibility entry, and `goal.mjs` load/redact the independently configured search credential before launching the SDK worker. The old Python `loopx_cycle` launcher explicitly rejects these tools; use the application host or CANN SDK path instead. Prepared runtime config contains only the registered tools. Read-capable child agents may use the three tools only when those tools are also admitted by the parent; analysis-only children have no tools.

`online_search({"query":"site:github.com Ascend scatter implementation"})` returns source URLs, titles and bounded snippets. `online_fetch({"url":"https://github.com/Ascend/pytorch"})` returns bounded text and response identity. Pages and snippets are untrusted external evidence, not executable instructions or benchmark evidence. Cite the actual source URL and retrieval time.

Use `online_search({"query":"exact_symbol_name","domains":["docs.example.org"]})` to constrain sources explicitly. Up to eight public hostnames are accepted, with IDNA/case normalization; a domain includes its subdomains, not lookalike suffixes. No domains are selected by default. The adapter appends a visible `site:` expression to the original query and independently filters returned URL hosts because providers may ignore that expression. All original response bytes and parsed results remain retained in `response.bin` and `text.txt`, including excluded results. `domain_filtered_count` records exclusions, and an empty filtered result is still a completed search, not a provider error. Domain filtering is not a judgment of relevance or source quality.

Search results explicitly identify `content_kind=search_snippets` and `source_bodies_fetched=false`. `online_fetch` separately identifies `content_kind=fetched_page_text`: only that operation has fetched the linked page body. A search snippet is not proof that the source page was opened or supports a technical claim. Read the actual page when its details matter.

Each successful search retains `search.json`, also returned as `search`, with the original and effective query, requested domains, exact request URL, response hash, parsed-results digest and result/filter counts. Bing RSS's channel link query and Brave/SearXNG's query field, where present, are exposed as `provider_query_echo`, including whether they match the effective request. These fields are provider-declared provenance, not independent relevance validation. A mismatched or absent echo remains visible without guessing the provider's intended query. A matching echo and HTTP 200 can still accompany unrelated results. Identical results across different queries do not by themselves establish an encoding, DNS, proxy or caching defect. No automatic query rewrite, relevance classifier, provider switch or source-body fetch is performed.

Both tools return an original `run` identity. Use `online_read({"run":"..."})` to inspect the manifest; add `path`, byte `offset` and `limit` to page through retained artifacts. `response.bin` is returned as base64, preserving original bytes. Text pages also include base64, so a UTF-8 character split across byte pages remains recoverable. `text.txt` retains the complete extracted text within the response-byte limit; result summaries are bounded. Hashes distinguish a complete response from a retained prefix. Nontext content is retained but not parsed. No JavaScript execution, login, cookies, proxy environment, or TLS-verification bypass is supported. An explicit `"http_proxy": "http://127.0.0.1:7897"` may select a local HTTP CONNECT transport. Only credential-free loopback proxy URLs are admitted; the proxy tunnels the already validated public destination IP, while TLS still verifies the original target hostname against the frozen CA bundle. The proxy setting and destination resolution policy are frozen, and each response records its transport. No proxy is inferred from environment variables.

Each fetch validates public addresses, connects to the checked IP with TLS hostname verification, and validates each redirect. Private/local addresses, credential URLs, unsafe ports and HTTP downgrade redirects are rejected. Search does not follow redirects or forward its credential elsewhere. Limits and endpoints are pinned with the driver. Existing attempt IDs are never reexecuted. Timeouts preserve `unknown`; inspect the original receipt before deciding whether to issue a new request. Gateway command receipts independently retain action/outcome and stdout/stderr identity.

Provider contracts were checked against [Brave Web Search documentation](https://api-dashboard.search.brave.com/app/documentation/web-search/get-started) and [SearXNG Search API documentation](https://docs.searxng.org/dev/search_api.html). Brave uses GET `/res/v1/web/search`, `q`/`count`, and `X-Subscription-Token`; SearXNG uses GET with `q` and `format=json`. These documented contracts do not constitute live provider qualification.

Brave empty-result handling follows its [official response schema](https://api-dashboard.search.brave.com/api-reference/web/search/get): `web` may be absent/null; when present, `web.results` is required. This adapter accepts absent/null web as empty only when `type=search` and `query.original` identify a valid search response. Other missing or malformed envelopes, including nullable/missing query metadata, are conservatively unavailable rather than asserted empty searches.
