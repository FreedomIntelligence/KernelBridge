"""KTP (k8s tenant) backend: one operator = one Job, even NPU, share-disk code."""
from __future__ import annotations

import base64
import json
import os
import re
import shlex
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import List

from .config import CAMPAIGN, Config, KTP_REPLICAS
from .job import AbLaunch, AbSpec, guard_npu, guard_path
from .parse import parse_ktp_job_id
from .share import Share, to_share_path
from .ssh import redact, scp_to, ssh


COHORT_LOCK_DIR = CAMPAIGN / ".cohort_locks"


def _ab_claim_path(spec: AbSpec, root: Path | None = None) -> Path:
    """Stable local claim for one user-visible A/B tag.

    KTP's list endpoint is eventually consistent. Retrying a submit because
    the first CLI call returned no JSON can otherwise create two jobs writing
    the same share-run directory.
    """
    return (root or COHORT_LOCK_DIR) / f"{job_name(spec, stamp=False)}.json"


def _reject_legacy_lock(path: Path) -> None:
    legacy_roots = (
        path.parent.with_name(".ab_claims"),
        path.parent.resolve().with_name("ab_claims"),
    )
    for legacy_root in legacy_roots:
        legacy = legacy_root / path.name
        if legacy.exists() or legacy.is_symlink():
            raise RuntimeError(
                f"AB_DEDUP_SKIP: legacy reservation at {legacy}; "
                "reconcile the existing job and complete the offline lock migration"
            )


def _acquire_ab_claim(spec: AbSpec, root: Path | None = None) -> Path | None:
    if os.environ.get("AB_FORCE_DUP") == "1":
        return None
    path = _ab_claim_path(spec, root)
    _reject_legacy_lock(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = {
        "op": spec.op,
        "tag": spec.tag,
        "reps": spec.reps,
        "arms": spec.arms,
        "state": "preparing",
        "created_at": int(time.time()),
        "pid": os.getpid(),
    }
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        try:
            old = json.loads(path.read_text())
            detail = f"state={old.get('state')} job_id={old.get('job_id') or '-'}"
        except Exception:
            detail = "existing unreadable claim"
        raise RuntimeError(
            f"AB_DEDUP_SKIP {spec.op}/{spec.tag}: {detail}; "
            "use a new tag, or AB_FORCE_DUP=1 only after checking KTP"
        ) from exc
    with os.fdopen(fd, "w") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")
    return path


def _update_ab_claim(path: Path | None, **fields: object) -> None:
    if path is None:
        return
    try:
        payload = json.loads(path.read_text())
    except Exception:
        payload = {}
    payload.update(fields)
    tmp = path.with_suffix(f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def job_name(spec: AbSpec, *, stamp: bool = False) -> str:
    s = f"ab-{spec.op}-{spec.tag}".lower()
    s = re.sub(r"[^a-z0-9-]+", "-", s).strip("-")
    s = s or "ab-job"
    if stamp:
        # Reserve room for the suffix. Appending then slicing could truncate
        # the whole timestamp for long names, giving parallel jobs identical
        # KTP names. A random suffix also avoids same-second collisions.
        suffix = f"-{uuid.uuid4().hex[:8]}"
        s = f"{s[:48 - len(suffix)].rstrip('-')}{suffix}"
    return s[:48]


def render_payload(spec: AbSpec, share_arms: dict[str, str], cfg: Config) -> str:
    for p in share_arms.values():
        guard_path(p, cfg)
    # Each arm is one argument to ab910.sh.  Quote the complete name:path
    # token so a valid share path (or a user supplied arm name) cannot split
    # the command or inject shell syntax.
    arm_args = " ".join(shlex.quote(f"{k}:{v}") for k, v in share_arms.items())
    out = f"{cfg.share_runs}/{spec.tag}"
    tools = f"{cfg.share_campaign}/r950_tools"
    return f"""#!/usr/bin/env bash
set -uo pipefail
export PYTHONUNBUFFERED=1
export AB_LOCAL=1
export AB_DETACH=0
export AB_DEVS="0 1"
export AB_CONC=2
export AB_CANN={shlex.quote(str(spec.cann))}
export CANN_BUILD_JOBS={int(spec.build_jobs if spec.build_jobs else 8)}
export CANN_BENCH_HOME={shlex.quote(spec.bench or cfg.share_bench)}
export AB_TAG={shlex.quote(spec.tag)}
export AB_OUT={shlex.quote(out)}
export TORCH_DEVICE_BACKEND_AUTOLOAD=0
# 🔴 KTP 并发竞态修复(2026-08-19)：每个 job 用独立 cann_bench_utils build 目录，
#    避免多容器同时编译共享目录造成 cmake 文件系统冲突(Device or resource busy)。
export CANN_BENCH_UTILS_BUILD_DIR="/tmp/cann_bench_utils_build_${{HOSTNAME:-unknown}}_$$"
mkdir -p "$AB_OUT"
if [ -f /usr/local/Ascend/cann-9.0.0/bin/setenv.bash ]; then
  set +u
  source /usr/local/Ascend/cann-9.0.0/bin/setenv.bash || true
  set -u
fi
export ASCEND_HOME_PATH="${{ASCEND_HOME_PATH:-/usr/local/Ascend/cann-9.0.0}}"
TOOLS={shlex.quote(tools)}
if [ ! -x "$TOOLS/ab910.sh" ]; then
  echo "⛔ missing $TOOLS/ab910.sh — run: python3 -m ab_sdk share sync" >&2
  exit 2
fi
if [ ! -d "$CANN_BENCH_HOME/scripts" ]; then
  echo "⛔ missing $CANN_BENCH_HOME" >&2
  exit 2
fi
cd "$TOOLS"
bash ./ab910.sh {shlex.quote(spec.op)} {int(spec.reps)} {arm_args} 2>&1 | tee "$AB_OUT/ab.log"
rc=${{PIPESTATUS[0]}}
if [ "$rc" -ne 0 ]; then
  echo "AB_FAILED tag={shlex.quote(spec.tag)} rc=$rc" >&2
  exit "$rc"
fi
echo AB_DONE tag={shlex.quote(spec.tag)} op={shlex.quote(spec.op)}
"""


def flag_submit_script(
    jobs: list[dict],
    *,
    image: str,
    replicas: int = KTP_REPLICAS,
) -> str:
    """Build one SSH script containing independently labelled KTP submits.

    KTP can reject one submit while accepting the following one.  Emitting an
    explicit result marker for every command lets the caller bind IDs by job
    name instead of assuming that the global order of ``ID:`` lines is stable.
    """
    if int(replicas) < 1:
        raise ValueError(f"replicas must be >= 1 (got {replicas})")
    lines = ["unset KTP_TOKEN", "set +e"]
    seen: set[str] = set()
    for j in jobs:
        name = str(j["name"])
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,47})", name):
            raise ValueError(f"invalid KTP job name {name!r}")
        if name in seen:
            raise ValueError(f"duplicate KTP job name {name!r}")
        seen.add(name)
        npu = int(j["npu"])
        guard_npu(npu, allow_wide=npu > 2)
        runtime = int(j["max_runtime"])
        if runtime < 1:
            raise ValueError(f"max_runtime must be >= 1 (got {runtime})")
        command = str(j["command"])
        if not command.strip():
            raise ValueError(f"empty command for KTP job {name!r}")
        submit = (
            "ktp submit"
            f" --name {shlex.quote(name)}"
            f" --image {shlex.quote(image)}"
            f" --npu {npu}"
            f" --cpu {shlex.quote(str(j['cpu']))}"
            f" --memory {shlex.quote(str(j['memory']))}"
            f" --max-runtime {runtime}"
            f" --replicas {int(replicas)}"
            " --framework PyTorch --job-type acjob"
            f" -c {shlex.quote(command)}"
            " </dev/null 2>&1"
        )
        lines.append(
            f"printf '%s\\n' {shlex.quote(f'AB_SUBMIT_BEGIN name={name}')}"
        )
        lines.append(f"submit_out=$({submit})")
        lines.append("submit_rc=$?")
        lines.append('printf "%s\\n" "$submit_out"')
        # Keep parsing on the remote side small and shell-only.  The Python
        # client still has a fallback parser for older remote wrappers.
        lines.append(
            "submit_id=$(printf '%s\\n' \"$submit_out\" | "
            "sed -n -E 's/.*ID:[[:space:]]*([0-9]+).*/\\1/p' | head -1)"
        )
        lines.append(
            "if [ -z \"$submit_id\" ]; then "
            "submit_id=$(printf '%s\\n' \"$submit_out\" | "
            "sed -n -E 's/.*\\\"id\\\"[[:space:]]*:[[:space:]]*\\\"?([0-9]+)\\\"?.*/\\1/p' | head -1); fi"
        )
        lines.append(
            f"printf 'AB_SUBMIT_RESULT name=%s rc=%s id=%s\\n' "
            f"{shlex.quote(name)} \"$submit_rc\" \"$submit_id\""
        )
    lines.append("echo SUBMIT_BUNDLE_OK")
    return "\n".join(lines)


def ab_out_from_command(command: str) -> str:
    """Recover AB_OUT from a KTP job command (base64 payload)."""
    m = re.search(r"printf '%s' '([A-Za-z0-9+/=]+)'", command or "")
    if not m:
        return ""
    try:
        payload = base64.b64decode(m.group(1)).decode("utf-8", "replace")
    except Exception:
        return ""
    m2 = re.search(r"^export AB_OUT=(.+)$", payload, re.M)
    if m2:
        return m2.group(1).strip().strip("'\"")
    m3 = re.search(r"^export AB_TAG=(.+)$", payload, re.M)
    if not m3:
        return ""
    from .config import SHARE_RUNS
    tag = m3.group(1).strip().strip("'\"")
    return f"{SHARE_RUNS}/{tag}"


def wrap_command(script: str) -> str:
    b64 = base64.b64encode(script.encode("utf-8")).decode("ascii")
    return (
        f"printf '%s' '{b64}' | base64 -d > /tmp/ab_payload.sh "
        f"&& chmod 700 /tmp/ab_payload.sh && bash /tmp/ab_payload.sh"
    )


def render_yaml(
    *,
    name: str,
    image: str,
    command: str,
    npu: int,
    cpu: str,
    memory: str,
    max_runtime: int,
    replicas: int = 1,
) -> str:
    cmd = " ".join(command.split())

    def scalar(value: object) -> str:
        text = str(value)
        # Keep the long-standing readable plain form for simple KTP values;
        # quote anything YAML could interpret or split (notably image URLs and
        # the shell command).
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]*", text):
            return text
        if re.fullmatch(r"[0-9]+", text):
            return f"'{text}'"
        return "'" + text.replace("'", "''") + "'"

    return (
        f"name: {scalar(name)}\n"
        f"framework: PyTorch\n"
        f"job-type: acjob\n"
        f"image: {scalar(image)}\n"
        f"cpu: {scalar(cpu)}\n"
        f"memory: {scalar(memory)}\n"
        f"npu: {int(npu)}\n"
        f"replicas: {int(replicas)}\n"
        f"max-runtime: {int(max_runtime)}\n"
        f"command: {scalar(cmd)}\n"
    )


class KtpBackend:
    def __init__(self, cfg: Config | None = None):
        self.cfg = cfg or Config()
        self.share = Share(self.cfg)

    def _ktp(self, args: List[str], timeout: int = 60) -> str:
        # ~/.ktp/env KTP_TOKEN can expire while ~/.ktp/config still holds a
        # valid token. Sourcing env overrides config and 401s every command.
        inner = "unset KTP_TOKEN; ktp " + " ".join(
            shlex.quote(a) for a in args
        ) + " </dev/null"
        r = ssh(inner, self.cfg, timeout=timeout)
        text = redact(r.text)
        # The demo1 sshd may return 255 after a successful command, but a
        # non-zero command status is still a KTP failure.  A connection
        # failure also returns 255 with no stdout, so do not hide that case.
        if r.rc not in (0, 255) or (r.rc == 255 and not r.stdout.strip()):
            raise RuntimeError(f"ktp {args[:4]} rc={r.rc}: {text[-1500:]}")
        return text

    def queues(self) -> str:
        return self._ktp(["queues"])

    def status(self) -> str:
        return self._ktp(["status"])

    def list(self, extra: List[str] | None = None) -> str:
        return self._ktp(["list", *(extra or [])])

    def get(self, job_id: str, fmt: str = "json") -> str:
        return self._ktp(["get", job_id, "-o", fmt])

    def logs(self, job_id: str, tail: int = 200) -> str:
        text = self._ktp(["logs", job_id, "--tail", str(int(tail))], timeout=90)
        if "No pods found" not in text and text.strip():
            return text
        fb = self._share_ab_log(job_id, tail)
        if fb:
            return fb
        return text

    def _share_ab_log(self, job_id: str, tail: int) -> str:
        """One SSH: ktp get → decode AB_OUT → tail share ab.log."""
        py = (
            "import json,re,base64,sys\n"
            "raw=sys.stdin.read()\n"
            "s,e=raw.find('{'),raw.rfind('}')\n"
            "d=json.loads(raw[s:e+1]) if s>=0 and e>s else {}\n"
            "cmd=str(d.get('command') or '')\n"
            "m=re.search(r\"printf '%s' '([A-Za-z0-9+/=]+)'\", cmd)\n"
            "if not m:\n"
            "    sys.exit(0)\n"
            "p=base64.b64decode(m.group(1)).decode('utf-8','replace')\n"
            "for line in p.splitlines():\n"
            "    if line.startswith('export AB_OUT='):\n"
            "        print(line.split('=',1)[1].strip().strip(chr(39)+chr(34)))\n"
            "        break\n"
        )
        r = ssh(
            "unset KTP_TOKEN; "
            f"p=$(ktp get {shlex.quote(job_id)} -o json 2>/dev/null | python3 -c {shlex.quote(py)}) && "
            f'if [ -n "$p" ]; then echo "# share $p/ab.log"; tail -n {int(tail)} "$p/ab.log"; fi',
            self.cfg,
            timeout=50,
        )
        body = (r.stdout or "").strip()
        if "# share " not in body:
            return ""
        return body + "\n"

    def stop(self, job_id: str) -> str:
        return self._ktp(["stop", job_id])

    def _prepare(self, spec: AbSpec, sync: bool, *, dry_run: bool = False) -> dict[str, str]:
        if sync and not dry_run:
            return self.share.sync_spec(spec)
        return {n: to_share_path(p, self.cfg) for n, p in spec.arms.items()}

    def submit_ab(
        self,
        spec: AbSpec,
        *,
        npu: int | None = None,
        sync: bool = True,
        dry_run: bool = False,
        allow_wide: bool = False,
        max_runtime: int | None = None,
        stamp_name: bool = False,
    ) -> AbLaunch:
        npu_n = guard_npu(npu if npu is not None else spec.npu, allow_wide=allow_wide)
        runtime = max_runtime if max_runtime is not None else spec.max_runtime
        claim = None if dry_run else _acquire_ab_claim(spec)
        submit_started = False
        try:
            share_arms = self._prepare(spec, sync, dry_run=dry_run)
            payload = render_payload(spec, share_arms, self.cfg)
            command = wrap_command(payload)
            name = job_name(spec, stamp=stamp_name)
            yaml_text = render_yaml(
                name=name,
                image=self.cfg.ktp_image,
                command=command,
                npu=npu_n,
                cpu=self.cfg.ktp_cpu,
                memory=self.cfg.ktp_memory,
                max_runtime=runtime,
            )
            yaml_remote = f"{self.cfg.share_jobs}/{name}.yaml"
            launch = AbLaunch(
                backend="ktp",
                spec=spec,
                log_path=f"{self.cfg.share_runs}/{spec.tag}/ab.log",
                yaml_path=yaml_remote,
                extra={"share_arms": share_arms, "npu": npu_n, "yaml": yaml_text},
            )
            if dry_run:
                launch.extra["dry_run"] = True
                launch.extra["command"] = command
                return launch
            self.share.mkdir()
            # Keep a YAML artifact for inspection, but use the flag interface for
            # the actual submission.  The cluster's YAML endpoint currently
            # rejects otherwise valid jobs with a max_runtime_minutes error.
            tmp_fd, tmp_name = tempfile.mkstemp(prefix=f"{name}-", suffix=".yaml")
            os.close(tmp_fd)
            tmp = Path(tmp_name)
            try:
                tmp.write_text(yaml_text)
                upload = scp_to(tmp, yaml_remote, self.cfg)
                launch.extra["yaml_upload_rc"] = upload.rc
                launch.extra["yaml_upload_stderr"] = upload.stderr[-400:]
            finally:
                tmp.unlink(missing_ok=True)
            submit_started = True
            _update_ab_claim(claim, state="submitting", submit_started_at=int(time.time()))
            out = self._ktp(
                [
                    "submit",
                    "--name", name,
                    "--image", self.cfg.ktp_image,
                    "--npu", str(npu_n),
                    "--cpu", self.cfg.ktp_cpu,
                    "--memory", self.cfg.ktp_memory,
                    "--max-runtime", str(int(runtime)),
                    "--replicas", str(KTP_REPLICAS),
                    "--framework", "PyTorch",
                    "--job-type", "acjob",
                    "-c", command,
                ],
                timeout=90,
            )
            launch.job_id = parse_ktp_job_id(out)
            if not launch.job_id:
                raise RuntimeError(f"ktp submit returned no job id: {out[-1500:]}")
            launch.extra["submit_out"] = out[-2000:]
            launch.extra.pop("yaml", None)
            _update_ab_claim(
                claim,
                state="submitted",
                job_id=launch.job_id,
                submitted_at=int(time.time()),
            )
            return launch
        except Exception as exc:
            if claim is not None:
                if submit_started:
                    # The remote submit may have succeeded even if SSH lost
                    # its response. Keep the claim so a blind retry cannot
                    # create another job sharing this AB_OUT directory.
                    _update_ab_claim(
                        claim,
                        state="submit_unknown",
                        error=f"{type(exc).__name__}: {str(exc)[:500]}",
                        failed_at=int(time.time()),
                    )
                else:
                    claim.unlink(missing_ok=True)
            raise

    def batch_ab(
        self,
        specs: List[AbSpec],
        *,
        parallel: int = 1,
        sync: bool = True,
        dry_run: bool = False,
        continue_on_error: bool = True,
        wait: bool = False,
    ) -> List[AbLaunch]:
        """One operator, one Job, npu=2.

        Default: submit sequentially (one ktp submit SSH at a time) and
        **return**. `wait=True` blocks until Completed — that hung every
        agent-tool `batch_ab` call (looked like sync/SSH deadlock).

        cmake is isolated under /tmp (private src + tasks symlink). Concurrent
        Running Jobs are cmake-safe. `parallel` is submit-wave size, not NPU.
        """
        if parallel < 1:
            raise ValueError("parallel >= 1")
        launches: List[AbLaunch] = []
        mapped: dict[str, dict[str, str]] = {}
        if sync and not dry_run:
            mapped = self.share.sync_specs(specs)
        for spec in specs:
            guard_npu(spec.npu)
            if dry_run or not sync:
                share_arms = {n: to_share_path(p, self.cfg) for n, p in spec.arms.items()}
            else:
                share_arms = mapped.get(spec.tag or spec.op) or {
                    n: to_share_path(p, self.cfg) for n, p in spec.arms.items()
                }
            payload = render_payload(spec, share_arms, self.cfg)
            command = wrap_command(payload)
            name = job_name(spec, stamp=True)
            yaml_text = render_yaml(
                name=name,
                image=self.cfg.ktp_image,
                command=command,
                npu=spec.npu,
                cpu=self.cfg.ktp_cpu,
                memory=self.cfg.ktp_memory,
                max_runtime=spec.max_runtime,
            )
            yaml_remote = f"{self.cfg.share_jobs}/{name}.yaml"
            launches.append(
                AbLaunch(
                    backend="ktp",
                    spec=spec,
                    yaml_path=yaml_remote,
                    log_path=f"{self.cfg.share_runs}/{spec.tag}/ab.log",
                    extra={
                        "share_arms": share_arms,
                        "yaml": yaml_text if dry_run else "",
                        "command": command,
                        "job_name": name,
                    },
                )
            )
        if dry_run:
            return launches
        for ln in launches:
            try:
                claim = _acquire_ab_claim(ln.spec)
            except RuntimeError as exc:
                if continue_on_error and "AB_DEDUP_SKIP" in str(exc):
                    ln.extra["submit_error"] = str(exc)
                    continue
                raise
            if claim is not None:
                ln.extra["claim"] = str(claim)
        if parallel > 1:
            print(f"KTP submit wave: parallel={parallel}", file=sys.stderr)
        submitted = 0
        batch_out_parts: List[str] = []
        i = 0
        while i < len(launches):
            wave = [
                ln for ln in launches[i:i + parallel]
                if not ln.extra.get("submit_error")
            ]
            if not wave:
                i += parallel
                continue
            flag_jobs = [
                {
                    "name": ln.extra["job_name"],
                    "command": ln.extra["command"],
                    "npu": ln.spec.npu,
                    "cpu": self.cfg.ktp_cpu,
                    "memory": self.cfg.ktp_memory,
                    "max_runtime": ln.spec.max_runtime,
                }
                for ln in wave
            ]
            ids, out = self._submit_flag_jobs(flag_jobs)
            batch_out_parts.append(out[-1500:])
            for j, ln in enumerate(wave):
                if j < len(ids):
                    ln.job_id = ids[j]
                    if ln.job_id:
                        submitted += 1
                if not ln.job_id:
                    ln.extra["submit_error"] = "ktp submit returned no job id"
                claim_s = ln.extra.get("claim")
                if claim_s:
                    _update_ab_claim(
                        Path(claim_s),
                        state="submitted" if ln.job_id else "submit_unknown",
                        job_id=ln.job_id,
                        submitted_at=int(time.time()),
                    )
            if wait:
                stop_rest = False
                for ln in wave:
                    if not ln.job_id:
                        ln.extra["wait"] = {"status": "no-id"}
                        if not continue_on_error:
                            stop_rest = True
                            break
                        continue
                    tmo = max(1800, int(ln.spec.max_runtime or 180) * 60)
                    rec = self.wait_done(ln.job_id, timeout_s=tmo)
                    ln.extra["wait"] = rec
                    if rec.get("timeout"):
                        stop_rest = True
                        break
                if stop_rest:
                    for rest in launches[i + parallel:]:
                        rest.extra["skipped"] = "previous_wait_timeout"
                    break
            i += parallel
        if launches:
            launches[0].extra["batch_out"] = "\n".join(batch_out_parts)[-3000:]
            launches[0].extra["submit_ids"] = submitted
            launches[0].extra["submit_mode"] = (
                "sequential" if parallel == 1 and wait else f"parallel={parallel},wait={wait}"
            )
        return launches

    def submit_raw(
        self,
        name: str,
        command: str,
        *,
        npu: int = 2,
        cpu: str = "4",
        memory: str = "8Gi",
        max_runtime: int = 5,
        dry_run: bool = False,
    ) -> dict:
        """Cheap Job (scale probe / warmup). Not an A/B."""
        guard_npu(npu, allow_wide=npu > 2)
        name = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")[:48]
        yaml_text = render_yaml(
            name=name,
            image=self.cfg.ktp_image,
            command=command,
            npu=npu,
            cpu=cpu,
            memory=memory,
            max_runtime=max_runtime,
        )
        if dry_run:
            return {"name": name, "npu": npu, "yaml": yaml_text, "job_id": ""}
        out = self._ktp(
            [
                "submit",
                "--name", name,
                "--image", self.cfg.ktp_image,
                "--npu", str(npu),
                "--cpu", cpu,
                "--memory", memory,
                "--max-runtime", str(int(max_runtime)),
                "--replicas", str(KTP_REPLICAS),
                "--framework", "PyTorch",
                "--job-type", "acjob",
                "-c", command,
            ],
            timeout=90,
        )
        job_id = parse_ktp_job_id(out)
        if not job_id:
            raise RuntimeError(f"ktp submit returned no job id: {out[-1500:]}")
        return {
            "name": name,
            "npu": npu,
            "job_id": job_id,
            "submit_out": out[-800:],
        }

    def _submit_flag_jobs(self, flag_jobs: List[dict]) -> tuple[List[str], str]:
        if not flag_jobs:
            return [], ""
        script = flag_submit_script(flag_jobs, image=self.cfg.ktp_image)
        r = ssh(script, self.cfg, timeout=max(180, 20 * max(1, len(flag_jobs))))
        out = redact(r.text)
        if r.rc not in (0, 255) or (r.rc == 255 and not r.stdout.strip()):
            raise RuntimeError(f"ktp submit bundle rc={r.rc}: {out[-1500:]}")

        # Bind IDs by the explicit marker rather than by global output order.
        # If one submit is rejected, the next successful ID must stay attached
        # to its own launch.
        marker_ids: dict[str, str] = {}
        for m in re.finditer(
            r"AB_SUBMIT_RESULT\s+name=(\S+)\s+rc=(\d+)\s+id=(\d*)", out
        ):
            name, _rc, job_id = m.groups()
            # Some KTP wrappers propagate sshd's historical 255 even when a
            # numeric ID was created. An ID is stronger evidence than that
            # wrapper status; only an empty ID means this launch failed.
            marker_ids[name] = job_id
        if not marker_ids:
            raise RuntimeError(f"ktp submit bundle returned no result markers: {out[-1500:]}")
        ids = [marker_ids.get(str(j["name"]), "") for j in flag_jobs]
        return ids, out

    def wait_done(
        self,
        job_id: str,
        *,
        timeout_s: int | None = None,
        poll_s: int = 25,
    ) -> dict:
        """Poll ktp get until Completed/Failed/Stopped. Empty id → no-id."""
        if not job_id:
            return {"job_id": "", "status": "no-id", "elapsed_s": 0}
        timeout_s = 7200 if timeout_s is None else int(timeout_s)
        poll_s = max(1, int(poll_s))
        terminal = {
            "completed", "complete", "failed", "stopped", "error",
            "succeeded", "success", "cancelled", "canceled", "terminated",
            "timeout", "timedout",
        }
        t0 = time.time()
        last = "unknown"
        while time.time() - t0 < timeout_s:
            try:
                last = self.job_status(job_id)
            except Exception as exc:  # noqa: BLE001
                last = f"error:{str(exc)[-80:]}"
            if str(last).lower() in terminal:
                tail = ""
                try:
                    tail = self.logs(job_id, tail=80)
                except Exception:
                    pass
                return {
                    "job_id": job_id,
                    "status": last,
                    "elapsed_s": round(time.time() - t0, 1),
                    "logs_tail": tail[-2000:],
                }
            remaining = timeout_s - (time.time() - t0)
            if remaining <= 0:
                break
            time.sleep(min(poll_s, remaining))
        return {
            "job_id": job_id,
            "status": last,
            "elapsed_s": round(time.time() - t0, 1),
            "timeout": True,
        }

    def job_status(self, job_id: str) -> str:
        raw = self.get(job_id)
        decoder = json.JSONDecoder()
        objects: List[dict[str, object]] = []
        # KTP prepends warnings in some environments. Decode each possible
        # object instead of using a greedy ``\{.*\}`` expression, which can
        # consume adjacent objects and make an otherwise valid response fail.
        for i, char in enumerate(raw):
            if char != "{":
                continue
            try:
                value, _ = decoder.raw_decode(raw[i:])
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                objects.append(value)
        for obj in reversed(objects):
            status = self._status_value(obj)
            if status:
                return status
        m = re.search(
            r"(?:status|state)\s*[:=]\s*['\"]?([A-Za-z_-]+)", raw, re.I
        )
        return m.group(1) if m else "unknown"

    @classmethod
    def _status_value(cls, value: object) -> str:
        if isinstance(value, dict):
            for key, item in value.items():
                if str(key).lower() in {"status", "state"} and item is not None:
                    if isinstance(item, (str, int, float)):
                        return str(item)
            for key in ("job", "data", "result", "detail", "metadata"):
                if key in value:
                    status = cls._status_value(value[key])
                    if status:
                        return status
        elif isinstance(value, list):
            for item in reversed(value):
                status = cls._status_value(item)
                if status:
                    return status
        return ""

    def scale_probe(
        self,
        n_jobs: int,
        *,
        npu: int = 2,
        hold_s: int = 40,
        cpu: str = "4",
        memory: str = "8Gi",
        stop: bool = True,
    ) -> dict:
        """Occupy n_jobs exclusive cards for hold_s and count Running vs Pending.

        demo1 16-card is 16 logical NPUs on ONE node and one ab910 at a time
        (kernel_eval gate). This measures cluster-wide exclusive cards.
        """
        if n_jobs < 1:
            raise ValueError("n_jobs >= 1")
        guard_npu(npu, allow_wide=npu > 2)
        stamp = int(time.time()) % 100000
        cmd = (
            "echo PROBE_START $(date -Iseconds); "
            "ls /dev/davinci* 2>/dev/null | wc -l; "
            "npu-smi info 2>/dev/null | head -16 || true; "
            f"sleep {int(hold_s)}; "
            "echo PROBE_OK"
        )
        runtime = max(3, (hold_s + 90) // 60)
        named: List[tuple[str, str]] = []
        for i in range(n_jobs):
            name = f"ab-scale-{stamp}-{i}"
            named.append((
                name,
                render_yaml(
                    name=name,
                    image=self.cfg.ktp_image,
                    command=cmd,
                    npu=npu,
                    cpu=cpu,
                    memory=memory,
                    max_runtime=runtime,
                ),
            ))
        t0 = time.time()
        jobs: List[dict] = []
        errors: List[str] = []
        try:
            flag_jobs = [
                {
                    "name": name,
                    "command": cmd,
                    "npu": npu,
                    "cpu": cpu,
                    "memory": memory,
                    "max_runtime": runtime,
                }
                for name, _ in named
            ]
            ids, _out = self._submit_flag_jobs(flag_jobs)
            for i, (name, _) in enumerate(named):
                jid = ids[i] if i < len(ids) else ""
                jobs.append({"name": name, "npu": npu, "job_id": jid})
            n_ids = sum(bool(x) for x in ids)
            if n_ids < n_jobs:
                errors.append(f"flag submit returned {n_ids} ids for {n_jobs}")
        except Exception as exc:  # noqa: BLE001
            errors.append(str(exc)[-400:])
        submit_s = round(time.time() - t0, 2)
        time.sleep(min(25, hold_s))
        counts = {"Running": 0, "Pending": 0, "Completed": 0, "Failed": 0, "other": 0}
        for rec in jobs:
            jid = rec.get("job_id") or ""
            if not jid:
                counts["other"] += 1
                rec["status"] = "no-id"
                continue
            try:
                st = self.job_status(jid)
            except Exception as exc:  # noqa: BLE001
                st = f"error:{str(exc)[-120:]}"
                errors.append(f"status {jid}: {str(exc)[-200:]}")
            rec["status"] = st
            if st in counts:
                counts[st] += 1
            else:
                counts["other"] += 1
        running = counts["Running"] + counts["Completed"]
        npu_live = running * npu
        if stop:
            for rec in jobs:
                jid = rec.get("job_id") or ""
                if jid and str(rec.get("status") or "").lower() in {"running", "pending"}:
                    try:
                        self.stop(jid)
                    except Exception:
                        pass
        return {
            "asked_jobs": n_jobs,
            "asked_npu": n_jobs * npu,
            "submitted": sum(bool(rec.get("job_id")) for rec in jobs),
            "submit_s": submit_s,
            "counts": counts,
            "npu_live": npu_live,
            "vs_demo1_16npu": round(npu_live / 16.0, 2) if npu_live else 0.0,
            "vs_serial_ab910": running,
            "errors": errors,
            "jobs": jobs,
        }
