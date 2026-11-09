#!/usr/bin/env python3
"""preflight.py —— 开工前 / 提交前跑一遍，把踩过的坑变成会报警的断言。

**每一条检查都对应一次真实事故**（见 `PITFALLS.md` 的完整清单和代价）。
文档挡不住重复犯错——这个文件挡得住。

用法：
    python3 preflight.py            # 全部检查
    python3 preflight.py --fast     # 跳过要下载冠军 zip 的那条（约 30s vs 3min）
    python3 preflight.py --fix      # 顺手把 livesrc910 刷成冠军代码

退出码：0 = 全过；1 = 有 🔴（别开工/别提交）；不为 0 时 CI/脚本应当中断。

⚠ 只读（除 --fix）。零 credit。不写 ledger.json / wavestate.json / cann.env。
"""
import os, sys, re, json, pathlib, subprocess, collections
import concurrent.futures as cf

HERE = pathlib.Path(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, str(HERE))
FAST = "--fast" in sys.argv
FIX = "--fix" in sys.argv

RED, YEL, OK = "🔴", "⚠ ", "✓ "
issues = []


def say(level, name, msg):
    print(f"{level}{name:32s} {msg}")
    if level == RED:
        issues.append(name)


def c_env():
    """token 是否已 source。事故：push910 --sync 直接 KeyError。"""
    try:
        import guard as G
        t = G.toks()
    except Exception as e:
        say(RED, "env/token", f"{e}"); return None
    say(OK if len(t) >= 15 else YEL, "env/token", f"{len(t)} 个账号")
    return G


def c_board(G):
    try:
        b = G.board()
    except Exception as e:
        say(RED, "榜面可达", str(e)[:80]); return None
    tot = sum(c["score"] for c in b.values())
    say(OK, "榜面可达", f"{len(b)} 行  总分 {tot:.2f}")
    return b


def c_passrate(G, b):
    """正确性欠债。权威口径是 component.passed_cases，不是 job。"""
    bad = [(c["operator_name"], k[1], c["passed_cases"], c["total_cases"])
           for k, c in b.items()
           if c.get("passed_cases") is not None and c["passed_cases"] < c["total_cases"]]
    debt = sum(30 * (t - p) / t for _, _, p, t in bad)
    say(YEL if bad else OK, "pass_rate 欠债",
        f"{len(bad)} 行没跑满，值 {debt:+.2f} 分" if bad else "106 行全部跑满")


def c_stale(G):
    """livesrc910 是否等于冠军。**本战役最贵的事故**：19/53 陈旧 ⇒ 全部 A/B 基线错。"""
    if FAST:
        say(YEL, "livesrc910 vs 冠军", "--fast 跳过（这是最贵的一条，别长期跳）"); return
    st = G.stale_vs_champion()
    if not st:
        say(OK, "livesrc910 vs 冠军", "53/53 一致"); return
    say(RED, "livesrc910 vs 冠军", f"{len(st)} 个算子不是冠军代码 ⇒ 本地 A/B 基线是错的")
    for op, diff in st[:8]:
        print(f"      {op:26s} {diff[:3]}")
    if FIX:
        print("      --fix：正在刷新 …")
        refresh(G, [op for op, _ in st])


def c_trees(G):
    """A1 扩展：**每一棵会被提交的快照树**都要比一次冠军，不只 livesrc910。

    🔴 为什么必须扩：A1「出处错误」已在三个不同目录上重演，而原来的 `c_stale`
    只看 `livesrc910` ⇒ 看不见 `reclaim910/`（22 个算子）、`generated/`（07-24 快照）、
    `candidates_hid/`（07-29 快照）。**08-02 那批 22-tag 回收就是从 `reclaim910/`
    把 pre-champion 旧代码又交了一遍，并赢下了 depthwise 的 hidden 行**
    ⇒ 那一行被锁在旧代码上，实测头寸 −2.7 ~ −7.5 分。

    语义按树分两种：
      * `livesrc910` 是**冠军镜像**，任何不一致都是 🔴（本地 A/B 基线错）——由 `c_stale` 管。
      * 其余是**历史快照**，与冠军不同是正常的；危险在于**从它们提交**。
        所以这里报「这棵树有几个算子的代码不是冠军」并标成「禁止从此树提交」。
    """
    if FAST:
        say(YEL, "快照树 vs 冠军", "--fast 跳过"); return
    import guard as GD
    for root in GD.SNAPSHOT_TREES:
        if root == "livesrc910" or not (HERE / root).is_dir():
            continue
        n = len([p for p in (HERE / root).iterdir() if p.is_dir()])
        st = [(op, d) for op, d in GD.stale_vs_champion(root=root) if d]
        if not st:
            say(OK, f"{root}/ vs 冠军", f"{n} 个算子全部等于冠军，从这里提交是安全的")
        else:
            say(YEL, f"{root}/ vs 冠军",
                f"{len(st)}/{n} 个算子**不是冠军代码** ⇒ 🔴 不要从 {root}/ 提交"
                f"（{', '.join(op for op, _ in st[:6])}{' …' if len(st) > 6 else ''}）")


def c_submitted_from(G):
    """扫 TRAJECTORY.jsonl：有没有真的从快照树提交过。这是 A1 的**事后**证据。"""
    import json
    import guard as GD
    tj = HERE / "TRAJECTORY.jsonl"
    if not tj.exists():
        say(YEL, "提交来源", "没有 TRAJECTORY.jsonl"); return
    bad, unknown = [], []
    snaps = [r for r in GD.SNAPSHOT_TREES if r != "livesrc910"]
    for ln in tj.read_text(errors="ignore").splitlines():
        try:
            d = json.loads(ln)
        except Exception:
            continue
        if d.get("kind") != "SUBMIT":
            continue
        src = d.get("src")
        if not src:
            # 🔴 不许把「没记 src」当成「不是从快照树来的」。08-02 那三发 depthwise
            #    （note=reclaim / reclaim2 / canon）**全都 src=None**，而它们正是
            #    从 reclaim910/ 提交的旧代码、并锁死了 depthwise 的 hidden 行。
            #    ⇒ 缺字段必须单独报「无法判定」，不能计入通过。
            unknown.append((d.get("t", "")[:16], d.get("op", ""), d.get("note", "")[:24]))
            continue
        for r in snaps:
            if f"{r}/" in str(src):
                bad.append((d.get("t", "")[:16], d.get("op", ""), r))
    if unknown:
        say(YEL, "提交来源(无 src)",
            f"{len(unknown)} 次提交没记 `src` ⇒ **无法判定**是否来自快照树。"
            f"note 里带 reclaim/canon 的尤其可疑，用 `hcode.py` 核代码身份")
        for t, op, note in unknown[:6]:
            print(f"      {t}  {op:26s} note={note}")
    if not bad:
        if not unknown:
            say(OK, "提交来源", "历史提交都不是从快照树来的")
        return
    say(YEL, "提交来源",
        f"有 {len(bad)} 次提交来自快照树 —— 这些行的 hidden 可能被锁在旧代码上，"
        f"跑 `python3 hcode.py` 核实")
    for t, op, r in bad[:8]:
        print(f"      {t}  {op:26s} <- {r}/")


def refresh(G, ops):
    import shutil, hashlib
    LIVE = HERE / "livesrc910"
    BK = HERE / "livesrc910.bak_prechampion"
    if not BK.exists():
        shutil.copytree(LIVE, BK)
    ldir = {G.norm(p.name): p for p in LIVE.iterdir() if p.is_dir()}
    b = G.board()
    for op in ops:
        c = b.get((G.norm(op), "standard"))
        if not c:
            continue
        z = G.champion_zip(c["job_id"])
        if z is None:
            print(f"      ✗ {op} 拉不到"); continue
        try:
            files = G.zip_op_files(z)
        except ValueError as e:
            print(f"      ✗ {op} {e}"); continue
        ld = ldir.get(G.norm(op))
        if ld is None:
            continue
        for p in ld.rglob("*"):
            if p.is_file():
                p.unlink()
        for rel, data in files.items():
            f = ld / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(data)
        print(f"      ✓ {op} 已刷新 {len(files)} 文件")


def c_memcpy():
    """host 侧 aclrtMemcpy ⇒ 反作弊整算子清零（20/20 也是 0 分）。"""
    hits = []
    for f in (HERE / "livesrc910").rglob("*"):
        if f.is_file() and f.suffix in (".cpp", ".h", ".hpp", ".cc"):
            if "aclrtMemcpy" in f.read_text(errors="ignore"):
                hits.append(str(f.relative_to(HERE)))
    # 现役冠军里出现 = 它在计分，说明是注释或非运行时路径（反作弊按运行时频次判）
    say(YEL if hits else OK, "aclrtMemcpy",
        f"{len(hits)} 个源文件提到（现役冠军⇒非运行时路径，但新写的代码里绝不能有）"
        if hits else "无")
    for h in hits[:5]:
        print(f"      {h}")


def c_arms():
    """臂目录是否有 PREDICT.md。没有预登记 = 事后合理化，不是实验。"""
    A = HERE / "arms"
    if not A.is_dir():
        say(YEL, "arms/PREDICT", "无 arms 目录"); return
    dirs = [d for d in A.iterdir() if d.is_dir() and not d.name.startswith("_")]
    no = [d.name for d in dirs if not (d / "PREDICT.md").exists()]
    say(YEL if no else OK, "arms/PREDICT",
        f"{len(dirs)-len(no)}/{len(dirs)} 个臂有预登记" + (f"，缺 {len(no)} 个" if no else ""))


def c_abq():
    """A/B 结果文件重复。事故：ABQ_RESULTS.txt 与 _A.txt 内容重复 ⇒ 赢家双计 +90 而非 +65。"""
    import hashlib
    fs = {p.name: hashlib.md5(p.read_bytes()).hexdigest()
          for p in HERE.glob("ABQ_RESULTS*.txt")}
    dup = collections.Counter(fs.values())
    bad = [n for n, h in fs.items() if dup[h] > 1]
    say(YEL if bad else OK, "ABQ 结果去重",
        f"内容重复: {bad}" if bad else f"{len(fs)} 个文件无重复")


def c_shell():
    """两个只在运行时才炸、`bash -n` 查不出的 shell 陷阱。

    ⚠ 只抓**裸 `$var`**。`${var}` 是**正确写法**，第一版正则把括号写成可选
    （`\\$\\{?…\\}?`）⇒ 三个脚本全部误报，而它们用的恰恰是正确形式。
    注释行跳过（那里往往正在**记录**这个坑）。
    """
    bad = []
    for p in list(HERE.glob("*.sh")) + list(HERE.glob("*.py")):
        for i, ln in enumerate(p.read_text(errors="ignore").splitlines(), 1):
            if ln.lstrip().startswith("#"):
                continue
            # 裸 $var 紧跟全角字符：UTF-8 续字节会被吃进变量名
            if re.search(r"\$[A-Za-z_][A-Za-z_0-9]*[　-〿＀-￯一-鿿]", ln):
                bad.append(f"{p.name}:{i} 裸 $var 后紧跟全角字符（写成 ${{var}}）")
            # zsh 的 $var:字母 会被当成 :a/:r/:h/:t 历史修饰符
            if re.search(r"\$[A-Za-z_][A-Za-z_0-9]*:[arhte]\b", ln):
                bad.append(f"{p.name}:{i} $var:字母 会被 zsh 当修饰符（写成 ${{var}}:）")
    say(RED if bad else OK, "shell 陷阱", "; ".join(bad[:4]) if bad else "无裸变量/冒号修饰符问题")


def c_hidden(G, b):
    """hidden 行是否落后 standard。补发零 credit、零下行，漏补 = 一直跑旧代码。"""
    lag = []
    for (op, cs), c in b.items():
        if cs != "standard":
            continue
        h = b.get((op, "hidden"))
        if not h:
            continue
        g = (G.local_time(c["updated_at"]) - G.local_time(h["updated_at"])).total_seconds() / 3600
        if g > 3:
            lag.append((g, c["operator_name"]))
    lag.sort(reverse=True)
    say(YEL if lag else OK, "hidden 陈旧",
        f"{len(lag)} 行落后 >3h（最久 {lag[0][0]:.0f}h {lag[0][1]}）⇒ 跑 hstale.py"
        if lag else "无")


def c_capacity(G):
    def one(a):
        s, d = W.api("/api/submissions/credits", os.environ[a])
        return (d or {}).get("credits", {}).get("remaining", 0) if s == 200 else 0
    import wave as W
    with cf.ThreadPoolExecutor(15) as ex:
        cr = sum(ex.map(one, G.toks()))
    say(OK if cr > 0 else YEL, "credit", f"合计 {cr}")


def main():
    print("=== preflight ===\n")
    G = c_env()
    if G is None:
        sys.exit(1)
    b = c_board(G)
    if b is None:
        sys.exit(1)
    c_passrate(G, b)
    c_hidden(G, b)
    c_stale(G)
    c_trees(G)
    c_submitted_from(G)
    c_memcpy()
    c_abq()
    c_arms()
    c_shell()
    c_capacity(G)
    print()
    if issues:
        print(f"🔴 {len(issues)} 项必须先解决: {issues}")
        sys.exit(1)
    print("全部通过。")


if __name__ == "__main__":
    main()
