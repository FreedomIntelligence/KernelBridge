#!/usr/bin/env python3
'Download completed-job artifacts using owner credentials.\n\nUsage: arti910.py JOB_ID [LOG_FILTER [MAX_LINES]]. Read-only; preserves raw logs.'
import os
import sys
import json
import pathlib
import concurrent.futures as cf

HERE = pathlib.Path(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, str(HERE))
os.environ.setdefault("CANN_BOARD", "910c")
import wave as W

OWNER_MAP = HERE / ".owner_map.json"


def _accts():
    a = [k for k in os.environ if k.startswith("GITCODE_TOKEN")]
    a.sort(key=lambda x: (len(x), x))
    return a


def owner_map(rebuild=False):
    """{user_id: 'GITCODE_TOKEN_N'}，落盘复用。"""
    if OWNER_MAP.exists() and not rebuild:
        try:
            return json.loads(OWNER_MAP.read_text())
        except Exception:
            pass

    def probe(a):
        s, d = W.api("/api/jobs?limit=1", os.environ[a])
        if s != 200 or not isinstance(d, dict):
            return None
        js = d.get("jobs") or d.get("items") or []
        return (js[0].get("user_id"), a) if js else None

    with cf.ThreadPoolExecutor(15) as ex:
        m = {u: a for u, a in filter(None, ex.map(probe, _accts()))}
    OWNER_MAP.write_text(json.dumps(m, indent=1))
    return m


def owner_token(jid, hint_tok=None):
    """返回能读这个 job 的 token；解析不出就返回 hint/第一个账号。"""
    probe = hint_tok or os.environ.get(_accts()[0], "")
    j = W.job_meta(jid, probe)
    uid = j.get("user_id")
    if not uid:
        return probe
    m = owner_map()
    if uid not in m:
        m = owner_map(rebuild=True)
    acct = m.get(uid)
    return os.environ.get(acct, probe) if acct else probe


def listing(jid, tok=None):
    """[(名字, url_or_None)]；HTTP 非 200 时返回 []。"""
    tok = tok or owner_token(jid)
    s, d = W.api(f"/api/jobs/{jid}/artifacts", tok)
    if s != 200:
        print(f"  ⚠ artifacts {s}: {str(d)[:120]}", file=sys.stderr)
        return []
    items = d.get("artifacts") or d.get("items") or d if isinstance(d, (list, dict)) else []
    if isinstance(items, dict):
        items = items.get("artifacts") or list(items.values())
    out = []
    for it in items:
        if isinstance(it, str):
            out.append((it, None))
        elif isinstance(it, dict):
            out.append((it.get("name") or it.get("path") or it.get("filename") or "?",
                        it.get("url") or it.get("download_url")))
    return out


def fetch(jid, want, tok=None):
    """按子串匹配取一个产物的文本内容。取不到返回 ''。"""
    tok = tok or owner_token(jid)
    for name, url in listing(jid, tok):
        if want.lower() not in name.lower():
            continue
        for path in (f"/api/jobs/{jid}/artifacts/{name}", url):
            if not path:
                continue
            p = path.split("cannbench.com", 1)[-1] if path.startswith("http") else path
            s, d = W.api(p, tok, raw=True)      # raw=True ⇒ 返回 bytes
            if s == 200 and d:
                if isinstance(d, bytes):
                    return d.decode("utf-8", errors="replace")
                return d if isinstance(d, str) else json.dumps(d, ensure_ascii=False)
    return ""


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(0)
    jid = sys.argv[1]
    tok = owner_token(jid)
    if len(sys.argv) == 2:
        for n, u in listing(jid, tok):
            print(f"  {n}")
    else:
        txt = fetch(jid, sys.argv[2], tok)
        lines = txt.splitlines()
        if len(sys.argv) > 3:
            lines = lines[:int(sys.argv[3])]
        print("\n".join(lines) if lines else "(空 / 取不到)")
