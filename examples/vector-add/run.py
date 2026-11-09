"""Prepare one bounded CPU example and start the original TypeScript product CLI."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

EXAMPLE = Path(__file__).resolve().parent
GOAL = """Optimize solution.js for CPU Float32 vector addition. The only source you may edit
is solution.js. Implement plain JavaScript function vectorAdd(a, b), where a and b
are equal-length Float32Array inputs containing finite values in [-1000,1000].
Return a Float32Array of the same length with each element exactly Math.fround(a[i]
+ b[i]); preserve both inputs. Standard Float32Array and Math operations are allowed. Lengths include zero and irregular tails up to
131072. No imports, network, host/process access, files, timers or hardcoded outputs.
The teaching baseline deliberately allocates pairs and an intermediate sums array.
Read the existing source through discovered MCP tools and use codemode to edit it.
Complete two distinct candidate iterations if the eight-turn budget permits:
edit the candidate, call run_experiment with a hypothesis, expected_delta and
cites:[], inspect public feedback, then refine and call run_experiment again.
Batch read/edit calls when useful. The pinned evaluator performs real correctness
and interleaved CPU timing, then independent confirmation; it may reject uncertain
changes. Do not edit evaluator/config/task/state or add source files. Finish with
a concise account of observed results. CPU time here is neither a device speedup
nor a benchmark or leaderboard result. No benchmark acquisition is required."""


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--python", type=Path)
    parser.add_argument("--expected-python-modules", type=Path)
    parser.add_argument("--python-path", type=Path)
    parser.add_argument("--agent-root", type=Path)
    parser.add_argument("--node", type=Path)
    parser.add_argument("--model", default="deepseek-flash")
    parser.add_argument("--replay", action="store_true", help="Enable the optional native replay plugin")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--prepare-only", action="store_true", help="Write inputs without model or evaluator calls")
    mode.add_argument("--offline-check", action="store_true", help="Check real CPU timings without model calls")
    return parser.parse_args()


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def runtime_paths(args: argparse.Namespace) -> dict[str, Path]:
    bundle = args.bundle.resolve(strict=True)
    installation = bundle / ".installation"
    runtime = bundle / "runtime"
    installed_node = None
    if (installation / "ready").is_file():
        runtime = Path((installation / "runtime").read_text().strip())
        installed_node = Path((installation / "node").read_text().strip())
        if not runtime.is_absolute() or not installed_node.is_absolute():
            raise ValueError("installation metadata requires absolute runtime and Node paths")
    node = args.node or installed_node or shutil.which("node")
    if not node:
        raise ValueError("Node >=22.19 is required; pass --node explicitly")
    paths = {
        "python": (args.python or runtime / "venv/bin/python").absolute(),
        "binding": (args.expected_python_modules or runtime / "expected.json").resolve(strict=True),
        "agent": (args.agent_root or bundle / "source/apps/agent").resolve(strict=True),
        "node": Path(node).resolve(strict=True),
    }
    # Preserve a venv interpreter's symlink: resolving it would lose its site-packages.
    if not paths["python"].is_file():
        raise ValueError("selected Python interpreter does not exist: " + str(paths["python"]))
    if not (paths["agent"] / "src/main.ts").is_file():
        raise ValueError("selected agent root has no src/main.ts")
    version = subprocess.check_output([str(paths["node"]), "--version"], text=True).strip()
    if tuple(int(part) for part in version.lstrip("v").split(".")[:2]) < (22, 19):
        raise ValueError("Node >=22.19 required, observed " + version)
    return paths


def import_root(binding: dict, explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit.resolve(strict=True)
    roots = set()
    for module in binding["modules"]:
        for uri in module["allowed_package_roots"]:
            url = urlsplit(uri)
            if url.scheme != "file" or url.netloc:
                raise ValueError("example needs local file module roots")
            roots.add(Path(unquote(url.path)).parent.resolve(strict=True))
    if len(roots) != 1:
        raise ValueError("binding has multiple import roots; pass --python-path explicitly")
    return roots.pop()


def configuration(paths: dict[str, Path], output: Path, binding: dict, pins: list[Path], replay: bool, model: str) -> dict:
    return {
        "python_modules": binding, "workspace": str(output / "workspace"),
        "state_dir": str(output / "state"), "credits": 180,
        "policy": {"max_steps": 8, "context_bytes": 65536},
        "providers": {"primary": {
            "provider": "openai", "base_url": "https://api.deepseek.com/v1", "model": model,
            "api_key_env": "DEEPSEEK_API_KEY", "max_tokens": 2048, "timeout": 60,
            "wire_options": {"native_turns": True, "token_parameter": "max_tokens",
                             "include_parallel_tool_calls": False, "tool_choice": "auto", "thinking": "disabled"},
        }},
        "pi_worker": {"node": str(paths["node"]), "root": str(paths["agent"]), "timeout": 420},
        "rsi": {
            "policy": {"seed": 17, "slots": 5, "exploration_fraction": 0.2,
                       "epsilon": 0.01, "delta": 0.03, "complexity_penalty": 0,
                       "max_units": 1, "minimum_pairs": 12, "integration_interval": 16, "holdout_limit": 2},
            "knowledge_query": None, "footprints": [], "task_category": "cpu-float32-vector-add",
            "holdout_task": False, "source_paths": ["solution.js"], "credits": 3, "timeout": 45,
            "adapter_argv": [str(path) for path in pins],
            "adapter_files": [{"path": str(path), "sha256": file_digest(path)} for path in pins],
            "replay_plugin": {"enabled": replay, "replay_on_compaction": replay},
        },
    }


def prepare(args: argparse.Namespace, paths: dict[str, Path]) -> tuple[Path, list[str], dict[str, str]]:
    output = args.output.resolve()
    if output.is_relative_to(args.bundle.resolve()) or output.is_relative_to(paths["agent"]):
        raise ValueError("--output must be outside the bundle and agent source trees")
    output.mkdir(parents=True, exist_ok=False)
    protected = output / "protected"
    protected.mkdir()
    adapter = protected / "adapter.mjs"
    shutil.copyfile(EXAMPLE / "adapter.mjs", adapter)
    runtime = protected / "runtime.json"
    write_json(runtime, {"agent_root": str(paths["agent"])})
    pins = [paths["node"], adapter, runtime]
    binding = json.loads(paths["binding"].read_text())
    config = configuration(paths, output, binding, pins, args.replay, args.model)
    write_json(output / "config.json", config)
    write_json(output / "task.json", {
        "id": "customer-cpu-vector-add", "scope": "standalone-cpu-example", "goal": GOAL,
        "seed_files": {"solution.js": (EXAMPLE / "baseline.js").read_text()},
        "contract": {"operator": "vector_add", "dtype": "float32", "maximum_length": 131072,
                     "correctness": "exact rounded sum; inputs preserved", "timing_unit": "ns"},
    })
    command = [str(paths["node"]), str(paths["agent"] / "src/main.ts"), "run",
               "--python", str(paths["python"]), "--expected-python-modules", str(paths["binding"]),
               "--config", str(output / "config.json"), "--task", str(output / "task.json"),
               "--timeout-ms", "450000"]
    environment = dict(os.environ)
    environment.pop("PYTHONPYCACHEPREFIX", None)
    environment["PYTHONPATH"] = str(import_root(binding, args.python_path))
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    write_json(output / "command.json", {"argv": command, "python_path": environment["PYTHONPATH"],
                                        "api_key_env": "DEEPSEEK_API_KEY"})
    for path in [adapter, runtime]:
        path.chmod(0o444)
    return output, command, environment


def offline_check(output: Path, config: dict) -> dict:
    baseline = {"path": "solution.js", "text": (EXAMPLE / "baseline.js").read_text()}
    candidate = {"path": "solution.js", "text": (EXAMPLE / "verification-candidate.js").read_text()}
    results = {}
    for operation in ["measure", "confirm"]:
        request = {
            "schema_version": "kernelcat.rsi-adapter/v1", "operation": operation,
            "task_id": "customer-cpu-vector-add", "experiment_id": "offline-evaluator-check",
            "unit_id": "whole-program", "baseline": [baseline], "keep": [baseline], "remove": [candidate],
            "sample_namespace": "offline-fixed-seed-17/" + operation,
        }
        response = subprocess.run(config["rsi"]["adapter_argv"], input=json.dumps({"arguments": request}),
                                  text=True, capture_output=True, timeout=45, check=False)
        (output / ("offline-" + operation + ".stdout.log")).write_text(response.stdout)
        (output / ("offline-" + operation + ".stderr.log")).write_text(response.stderr)
        if response.returncode:
            raise ValueError("offline " + operation + " failed; inspect "
                             + str(output / ("offline-" + operation + ".stderr.log")))
        results[operation] = json.loads(response.stdout)["data"]
    for result in results.values():
        measurement = result["rsi"]["measurement"]
        if measurement["keep_correct"] is not True or measurement["remove_correct"] is not True:
            raise ValueError("offline CPU evaluator correctness failed")
    results["model_calls"] = 0
    results["interpretation"] = "Evaluator smoke using a hand-written candidate; no model-quality or device-speed claim"
    write_json(output / "offline-check.json", results)
    return {"mode": "offline-check", "report": str(output / "offline-check.json"), "model_calls": 0}


def start(output: Path, command: list[str], environment: dict[str, str]) -> int:
    with (output / "stdout.log").open("w") as stdout, (output / "stderr.log").open("w") as stderr:
        result = subprocess.run(command, env=environment, cwd=output, stdout=stdout, stderr=stderr, timeout=470, check=False)
    text = (output / "stdout.log").read_text().strip()
    if text:
        print(text)
    if result.returncode:
        print("Product failed; retain original state and inspect " + str(output / "stderr.log"), file=sys.stderr)
        return result.returncode
    write_json(output / "result.json", json.loads(text))
    return 0


def main() -> int:
    args = arguments()
    if not args.prepare_only and not args.offline_check and not os.environ.get("DEEPSEEK_API_KEY"):
        raise ValueError("export DEEPSEEK_API_KEY before a live run; --offline-check and --prepare-only need no key")
    paths = runtime_paths(args)
    output, command, environment = prepare(args, paths)
    if args.prepare_only:
        print(json.dumps({"mode": "prepared", "output": str(output), "command": str(output / "command.json")}))
        return 0
    if args.offline_check:
        print(json.dumps(offline_check(output, json.loads((output / "config.json").read_text()))))
        return 0
    return start(output, command, environment)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print("vector-add example: " + str(error), file=sys.stderr)
        raise SystemExit(1) from error
