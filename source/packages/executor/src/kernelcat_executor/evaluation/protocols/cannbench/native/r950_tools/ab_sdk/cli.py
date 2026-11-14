"""CLI: python3 -m ab_sdk  /  ./abctl"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .client import AbClient
from .job import AbSpec
from .parse import delta, parse_runs, summarize


def _parse_arm(s: str) -> tuple[str, str]:
    if ":" not in s:
        raise argparse.ArgumentTypeError(f"arm must be name:/path, got {s!r}")
    name, src = s.split(":", 1)
    if not name or not src:
        raise argparse.ArgumentTypeError(f"arm must be name:/path, got {s!r}")
    return name, src


def _spec_from_args(ns: argparse.Namespace) -> AbSpec:
    arms = dict(ns.arm)
    return AbSpec(
        op=ns.op,
        reps=ns.reps,
        arms=arms,
        tag=ns.tag or "",
        cann=ns.cann,
        npu=ns.npu,
        max_runtime=ns.max_runtime,
    )


def _load_jobs(path: Path) -> list[AbSpec]:
    raw = path.read_text(encoding="utf-8")
    data = json.loads(raw)
    if isinstance(data, dict):
        data = data.get("jobs") or data.get("specs") or [data]
    return [AbSpec.from_mapping(x) for x in data]


def _add_ab_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("op", help="CamelCase or snake operator")
    p.add_argument("reps", type=int)
    p.add_argument("arm", nargs="+", type=_parse_arm, help="name:/abs/src")
    p.add_argument("--tag", default="")
    p.add_argument("--cann", default="9")
    p.add_argument("--npu", type=int, default=2)
    p.add_argument("--max-runtime", type=int, default=180)
    p.add_argument("--dry-run", action="store_true")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(
        prog="ab_sdk",
        description="Configured-host A/B and KTP job submit SDK",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_ab = sub.add_parser("ab", help="submit one operator A/B")
    p_ab.add_argument(
        "--backend",
        choices=("auto", "ktp", "demo1", "demo2"),
        default="auto",
        help="auto: a configured idle host, then configured KTP",
    )
    p_ab.add_argument("--devs", default="", help="logical device IDs, e.g. 0,1")
    p_ab.add_argument("--detach", action="store_true", default=True)
    p_ab.add_argument("--no-detach", action="store_false", dest="detach")
    p_ab.add_argument("--no-sync", action="store_true")
    _add_ab_flags(p_ab)

    p_batch = sub.add_parser(
        "batch",
        help="KTP: N operators, one Job each, npu=2; cmake-isolated so --parallel N is safe",
    )
    p_batch.add_argument("jobs", help="JSON list of {op,reps,arms}")
    p_batch.add_argument(
        "--parallel", type=int, default=1,
        help="submit-wave size (default 1 SSH at a time; jobs may overlap once queued)",
    )
    p_batch.add_argument(
        "--wait", action="store_true",
        help="block until each Job Completed (minutes; hangs short agent tools)",
    )
    p_batch.add_argument("--dry-run", action="store_true")
    p_batch.add_argument("--no-sync", action="store_true")

    p_q = sub.add_parser("queues", help="ktp queues")
    p_st = sub.add_parser("status", help="ktp status + demo1/demo2 kernel_eval")
    p_ls = sub.add_parser("list", help="ktp list")
    p_get = sub.add_parser("get", help="ktp get JOB")
    p_get.add_argument("job_id")
    p_logs = sub.add_parser("logs", help="ktp logs JOB")
    p_logs.add_argument("job_id")
    p_logs.add_argument("--tail", type=int, default=200)
    p_stop = sub.add_parser("stop", help="ktp stop JOB")
    p_stop.add_argument("job_id")
    p_wait = sub.add_parser("wait", help="poll ktp job until Completed/Failed")
    p_wait.add_argument("job_id")
    p_wait.add_argument("--timeout", type=int, default=7200)
    p_wait.add_argument("--poll", type=int, default=25)

    p_share = sub.add_parser("share", help="mkdir share + copy cann-bench + tools")
    p_share.add_argument("action", choices=("sync", "bench", "mkdir"))

    p_parse = sub.add_parser("parse", help="parse an ab910 log into min-over-reps")
    p_parse.add_argument("log")

    p_cases = sub.add_parser("cases", help="public catalog + per-case A/B / official job")
    p_cases.add_argument("op", nargs="?", help="operator (catalog if no --run/--job)")
    p_cases.add_argument("--run", help="share run tag")
    p_cases.add_argument("--job", help="official job_id")
    p_cases.add_argument("--file", action="append", default=[],
                         help="arm=path/to/cann_final_eval.json, repeatable")

    p_cap = sub.add_parser("capacity", help="configured host + KTP fields (live KE/queue)")
    p_cap.add_argument("--offline", action="store_true")
    p_plan = sub.add_parser("plan", help="fill Job fields for N operators")
    p_plan.add_argument("-n", type=int, default=1)
    p_plan.add_argument(
        "--backend",
        choices=("auto", "ktp", "demo1", "demo2", "fleet"),
        default="ktp",
    )
    p_plan.add_argument(
        "--parallel",
        type=int,
        default=None,
        help="KTP submit-wave size (default 1)",
    )
    p_fleet = sub.add_parser("fleet", help="configured hosts + remaining operators on KTP")
    p_fleet.add_argument("-n", type=int, default=4)
    p_fleet.add_argument("--parallel", type=int, default=None)
    p_host = sub.add_parser("host", help="probe / bootstrap configured hosts")
    p_host.add_argument("action", choices=("probe", "bootstrap"))
    p_host.add_argument("name", nargs="?", default="demo2")
    p_warm = sub.add_parser("warmup", help="one SSH inventory + stamp-skip sync")
    p_probe = sub.add_parser("probe", help="how many npu:2 Jobs go Running")
    p_probe.add_argument("-n", type=int, default=8, help="jobs (each npu=2)")
    p_probe.add_argument("--hold", type=int, default=40)
    p_probe.add_argument("--npu", type=int, default=2)
    p_probe.add_argument("--keep", action="store_true", help="do not stop probes")

    ns = parser.parse_args(argv)
    client = AbClient()

    if ns.cmd == "ab":
        spec = _spec_from_args(ns)
        kw: dict = {
            "sync": not ns.no_sync,
            "dry_run": ns.dry_run,
            "detach": ns.detach,
        }
        if ns.devs:
            kw["devices"] = tuple(int(x) for x in ns.devs.split(",") if x)
        try:
            launch = client.ab(spec, backend=ns.backend, **kw)
        except RuntimeError as exc:
            if str(exc).startswith("AB_DEDUP_SKIP"):
                print(str(exc), file=sys.stderr)
                return 3
            raise
        print(json.dumps(launch.to_dict(), ensure_ascii=False, indent=2))
        if launch.extra.get("yaml") and ns.dry_run:
            print("--- yaml ---")
            print(launch.extra["yaml"])
        return 0

    if ns.cmd == "batch":
        specs = _load_jobs(Path(ns.jobs))
        launches = client.batch(
            specs,
            parallel=ns.parallel,
            wait=ns.wait,
            sync=not ns.no_sync,
            dry_run=ns.dry_run,
        )
        print(json.dumps([x.to_dict() for x in launches], ensure_ascii=False, indent=2))
        return 0

    if ns.cmd == "queues":
        print(client.ktp.queues())
        return 0
    if ns.cmd == "status":
        for name, be in (("demo1", client.demo1), ("demo2", client.demo2)):
            ke = be.kernel_eval_count()
            print(f"{name} kernel_eval={ke}")
            if ke > 0:
                print(be.kernel_eval_lines())
        print(client.ktp.status())
        return 0
    if ns.cmd == "host":
        from .hosts import bootstrap_demo2, probe_host, rebuild_eval_env
        if ns.action == "bootstrap":
            if ns.name in ("all", "both"):
                rec = {
                    "demo1": rebuild_eval_env("demo1"),
                    "demo2": rebuild_eval_env("demo2"),
                }
                print(__import__("json").dumps(rec, ensure_ascii=False, indent=2))
                ok = all(v.get("rebuild_ok") or v.get("ok") for v in rec.values())
                return 0 if ok else 1
            rec = rebuild_eval_env(ns.name) if ns.name in ("demo1", "demo2") else probe_host(ns.name)
        else:
            rec = probe_host(ns.name)
        print(json.dumps(rec, ensure_ascii=False, indent=2))
        return 0 if rec.get("ok") or rec.get("bootstrapped") else 1
    if ns.cmd == "list":
        print(client.ktp.list())
        return 0
    if ns.cmd == "get":
        print(client.ktp.get(ns.job_id))
        return 0
    if ns.cmd == "logs":
        print(client.ktp.logs(ns.job_id, tail=ns.tail))
        return 0
    if ns.cmd == "stop":
        print(client.ktp.stop(ns.job_id))
        return 0
    if ns.cmd == "wait":
        rec = client.wait(ns.job_id, timeout_s=ns.timeout, poll_s=ns.poll)
        print(json.dumps(rec, ensure_ascii=False, indent=2))
        return 0 if not rec.get("timeout") and rec.get("status") != "no-id" else 1
    if ns.cmd == "share":
        if ns.action == "mkdir":
            client.share.mkdir()
        elif ns.action == "bench":
            client.share.ensure_bench()
        else:
            client.share.sync_tools()
            client.share.ensure_bench()
        print(f"share {ns.action} done -> {client.cfg.share_root}")
        return 0
    if ns.cmd == "capacity":
        print(json.dumps(client.capacity(live=not ns.offline), ensure_ascii=False, indent=2))
        return 0
    if ns.cmd == "plan":
        print(json.dumps(
            client.plan(
                ns.n,
                backend=ns.backend,
                live=False,
                parallel=ns.parallel,
            ),
            ensure_ascii=False, indent=2,
        ))
        return 0
    if ns.cmd == "fleet":
        print(json.dumps(
            client.plan(ns.n, backend="fleet", parallel=ns.parallel),
            ensure_ascii=False, indent=2,
        ))
        return 0
    if ns.cmd == "warmup":
        print(json.dumps(client.warmup(), ensure_ascii=False, indent=2))
        return 0
    if ns.cmd == "probe":
        rec = client.ktp.scale_probe(
            ns.n, npu=ns.npu, hold_s=ns.hold, stop=not ns.keep,
        )
        print(json.dumps(rec, ensure_ascii=False, indent=2))
        return 0
    if ns.cmd == "cases":
        if ns.job:
            print(json.dumps(client.pull_job(ns.job), ensure_ascii=False, indent=2))
            return 0
        if ns.run:
            print(json.dumps(client.pull_run(ns.run), ensure_ascii=False, indent=2))
            return 0
        if ns.file:
            files = {}
            for item in ns.file:
                if "=" not in item:
                    raise SystemExit("--file arm=/path/to/cann_final_eval.json")
                k, v = item.split("=", 1)
                files[k] = v
            op = ns.op or "Softmax"
            print(json.dumps(client.analyze_eval_files(files, op), ensure_ascii=False, indent=2))
            return 0
        if not ns.op:
            raise SystemExit("cases <op> | --run TAG | --job job_xxx | --file arm=json")
        print(json.dumps(client.cases_catalog(ns.op), ensure_ascii=False, indent=2))
        return 0
    if ns.cmd == "parse":
        text = Path(ns.log).read_text(errors="replace")
        lines = parse_runs(text)
        summ = summarize(lines)
        print(json.dumps(
            {
                "n": len(lines),
                "arms": {k: {"n_clean": v.n_clean, "min_score": v.min_score, "scores": v.scores} for k, v in summ.items()},
                "delta": delta(summ),
            },
            ensure_ascii=False,
            indent=2,
        ))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
