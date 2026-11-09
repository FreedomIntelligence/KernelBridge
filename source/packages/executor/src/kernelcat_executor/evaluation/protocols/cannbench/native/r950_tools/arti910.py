#!/usr/bin/env python3
"""arti910.py —— 按 job_id 拉取评测产物日志（compile / correctness / performance / RESULT.html）。

🔴 为什么必须有它（2026-08-05，这条教训烧了 9 发提交 + 大半天）：

`invalid_evaluation_results` 连拒 9 发，我对着 `error_code` / `stage_summary` /
`selected_operators` 反解了几个小时，**连续判错六次**：
    ① results 空 ⇒「从未评测」        ④ 只读端点 503 ⇒「后端宕机」
    ② has_results=True ⇒「改包」      ⑤ 429         ⇒「用户级限流」
    ③ snake 名 ⇒「体例错」            ⑥ 结构 diff 一致 ⇒「平台 bug，我们无可修」
**答案一直明文写在 `correctness.log` 第 30 行**（模板的 `cann_bench/__init__.py` 写死了
占位算子 `add`/`sqrt`，runner 扫不到本算子就回落成评测全部 53 个）。

⇒ **规则：拿到任何失败 job，第一步是把它的日志读完，再做任何机制推断。**
   比读任何结构化字段都便宜、都直接。

## 另一个它解决的问题：老 job 的 `results` 是空的

`job_meta()` 对新 job 带完整逐 case 数据，但**老 job（实测 07-24 的）`results` 是 `[]`**。
要拿老行的逐 case 失败原因，只能走这里的 `correctness.log`。

## 权限：artifacts 认属主 token，不是任意 token

非属主一律 `403 access denied`。本模块用 `.owner_map.json`（`user_id -> GITCODE_TOKEN_N`）
自动解析属主；映射不存在时会现场重建（每账号一次 `/api/jobs?limit=1`，零 credit）。

用法：
    python3 arti910.py <job_id>                 # 列出可用产物
    python3 arti910.py <job_id> correctness     # 打印某个日志（子串匹配）
    python3 arti910.py <job_id> correctness 80  # 只打印前 80 行

    import arti910; arti910.fetch("job_xxx", "correctness")   # -> 文本

⚠ 只读，零 credit，不写 ledger.json / wavestate.json。
"""
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
