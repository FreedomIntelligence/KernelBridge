"""Parse ab910.sh stdout and ktp submit text."""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field

RUN_RE = re.compile(
    r"^\s+(\S+)\s+#(\d+)\s+dev(\d+)\s+"
    r"计时\s+(\d+)/(\d+)\s+过\s+(\d+)/(\d+)\s+"
    r"分=([0-9.]+)\s+fn=([0-9.]+)\s+sp=([0-9.]+)(.*)$"
)
KTP_ID_RE = re.compile(r"ID:\s+(\d+)")
DETACH_RE = re.compile(r"远端日志\s+(\S+)")
KE_RE = re.compile(r"kernel_eval 进程.*?(\d+)|KE_COUNT=(\d+)|还有\s+(\d+)\s+个 kernel_eval")


@dataclass
class RunLine:
    arm: str
    rep: int
    dev: int
    timed: int
    n_cases: int
    passed: int
    total: int
    score: float
    fn: float
    sp: float
    timing_missing: bool = False
    correctness_fail: bool = False
    raw: str = ""

    @property
    def clean(self) -> bool:
        return (
            not self.timing_missing
            and not self.correctness_fail
            and self.n_cases > 0
            and self.timed == self.n_cases
            and self.passed == self.total
        )


@dataclass
class ArmSummary:
    arm: str
    n_clean: int
    min_score: float | None
    scores: list[float] = field(default_factory=list)
    lines: list[RunLine] = field(default_factory=list)


def parse_runs(text: str) -> list[RunLine]:
    out: list[RunLine] = []
    for line in text.splitlines():
        m = RUN_RE.match(line)
        if not m:
            continue
        tail = m.group(11) or ""
        out.append(
            RunLine(
                arm=m.group(1),
                rep=int(m.group(2)),
                dev=int(m.group(3)),
                timed=int(m.group(4)),
                n_cases=int(m.group(5)),
                passed=int(m.group(6)),
                total=int(m.group(7)),
                score=float(m.group(8)),
                fn=float(m.group(9)),
                sp=float(m.group(10)),
                timing_missing="计时缺失" in tail,
                correctness_fail="正确性退化" in tail,
                raw=line.rstrip(),
            )
        )
    return out


def summarize(lines: list[RunLine]) -> dict[str, ArmSummary]:
    by: dict[str, list[RunLine]] = defaultdict(list)
    for ln in lines:
        by[ln.arm].append(ln)
    out: dict[str, ArmSummary] = {}
    for arm, group in by.items():
        clean = [x.score for x in group if x.clean]
        out[arm] = ArmSummary(
            arm=arm,
            n_clean=len(clean),
            min_score=min(clean) if clean else None,
            scores=clean,
            lines=group,
        )
    return out


def delta(summary: dict[str, ArmSummary], base: str = "base") -> dict[str, float | None]:
    b = summary.get(base)
    base_min = b.min_score if b else None
    out: dict[str, float | None] = {}
    for arm, s in summary.items():
        if arm == base:
            continue
        if base_min is None or s.min_score is None:
            out[arm] = None
        else:
            out[arm] = round(s.min_score - base_min, 4)
    return out


def parse_ktp_job_id(text: str) -> str:
    m = KTP_ID_RE.search(text)
    if m:
        return m.group(1)
    m = re.search(r'"id"\s*:\s*"?(\d+)"?', text)
    return m.group(1) if m else ""


def parse_detach_log(text: str) -> str:
    m = DETACH_RE.search(text)
    return m.group(1) if m else ""
