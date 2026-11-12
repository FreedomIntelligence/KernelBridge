# Public online research tools

These are KernelBridge provider adapters, not ports of Codex or Claude hosted search. They make public HTTPS GET requests for an explicit query or URL. They do not forward chat history, automatically read workspace files, or call a model to summarize a page.

Set `online_research` in an ordinary codegen config, a CANN profile, or the input consumed by the operator campaign preparation helper:

```json
{"online_research": true}
```

`true` selects `duckduckgo_html` at `https://html.duckduckgo.com/html/`. This no-key HTML interface is best effort, not a stable general-search API. Challenges, changed markup, unavailable network access, and failed responses produce an explicit unavailable outcome. Configuration alone does not establish a successful live search.

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

An instance's JSON access, coverage, rate limits and availability are its operator's responsibility. Local/private instances and authenticated URL parameters are outside this adapter's public access scope. Brave and DuckDuckGo endpoints are fixed; SearXNG's explicit endpoint is frozen in the prepared config. No provider endpoint is chosen by model input.

`prepare(profile, output, python)` in `prepare.py` performs no network or model work. It returns:

- `business_tools`: `online_search`, `online_fetch`, `online_read`, with driver/config SHA-256 pins and bounded argument schemas.
- `online_research`: frozen driver configuration for artifact/provenance use. Do not add this field to the strict worker runtime config.
- `resources`: the provider, endpoint, credential variable name, access scope, pins and usage descriptor for an admitted resource map.

Merge `business_tools` into existing tools; duplicate names are an error. Ordinary codegen and CANN preparation integrate this helper automatically when configured. CANN `run.mjs`, its `--config` compatibility entry, and `goal.mjs` load/redact the independently configured search credential before launching the SDK worker. The old Python `loopx_cycle` launcher explicitly rejects these tools; use the application host or CANN SDK path instead. Prepared runtime config contains only the registered tools. Read-capable child agents may use the three tools only when those tools are also admitted by the parent; analysis-only children have no tools.

`online_search({"query":"site:github.com Ascend scatter implementation"})` returns source URLs, titles and bounded snippets. `online_fetch({"url":"https://github.com/Ascend/pytorch"})` returns bounded text and response identity. Pages and snippets are untrusted external evidence, not executable instructions or benchmark evidence. Cite the actual source URL and retrieval time.

Both tools return an original `run` identity. Use `online_read({"run":"..."})` to inspect the manifest; add `path`, byte `offset` and `limit` to page through retained artifacts. `response.bin` is returned as base64, preserving original bytes. Text pages also include base64, so a UTF-8 character split across byte pages remains recoverable. `text.txt` retains the complete extracted text within the response-byte limit; result summaries are bounded. Hashes distinguish a complete response from a retained prefix. Nontext content is retained but not parsed. No JavaScript execution, login, cookies, proxy environment, or TLS-verification bypass is supported.

Each fetch validates public addresses, connects to the checked IP with TLS hostname verification, and validates each redirect. Private/local addresses, credential URLs, unsafe ports and HTTP downgrade redirects are rejected. Search does not follow redirects or forward its credential elsewhere. Limits and endpoints are pinned with the driver. Existing attempt IDs are never reexecuted. Timeouts preserve `unknown`; inspect the original receipt before deciding whether to issue a new request. Gateway command receipts independently retain action/outcome and stdout/stderr identity.

Provider contracts were checked against [Brave Web Search documentation](https://api-dashboard.search.brave.com/app/documentation/web-search/get-started) and [SearXNG Search API documentation](https://docs.searxng.org/dev/search_api.html). Brave uses GET `/res/v1/web/search`, `q`/`count`, and `X-Subscription-Token`; SearXNG uses GET with `q` and `format=json`. These documented contracts do not constitute live provider qualification.

Brave empty-result handling follows its [official response schema](https://api-dashboard.search.brave.com/api-reference/web/search/get): `web` may be absent/null; when present, `web.results` is required. This adapter accepts absent/null web as empty only when `type=search` and `query.original` identify a valid search response. Other missing or malformed envelopes, including nullable/missing query metadata, are conservatively unavailable rather than asserted empty searches.
