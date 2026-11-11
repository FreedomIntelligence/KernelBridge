# Local application-session acceptance

For the ordinary codegen demo, use the current checkout's root launcher in a real terminal:

```sh
python3 -B validation/tui/demo.py
```

It opens an idle editor and makes no model request until input. Enter “读取 solution.py，把 x = 1 改成 x = 2，再用 bash 检查。” The provider is a fixed local script, not a real model. No budget, Goal, Trial or evaluator is enabled. The demo refuses a stale launcher from an earlier bundle. `--output DIR --resume` restores a previous codegen demo; the exact command is printed on exit.

For the explicit isolated experiment demo, use an installed bundle:

```sh
BUNDLE=/absolute/installed/KernelBridge
"$BUNDLE/runtime/venv/bin/python" -B validation/tui/demo.py --experiment --launcher "$BUNDLE/kernelagent"
```

This opens the real Pi TUI with a scripted localhost provider. The first turn reads, edits and evaluates a tiny Python fixture; subsequent replies are canned text, not a real model. Try Chinese input, `/goal 目标内容`, `/goal`, `/goal pause`, `/goal resume`, `/goal clear`, `/help`, `/latest`, `!cat solution.py`, `/compact`, `/context reset`, and `/quit`. No NPU or model credentials are needed. Exiting stops the demo's fixture server and host while retaining the session, source and budget. Copy the printed resume command to restart the fixture and attach the same session. Only one demo process may own that directory at a time.

Run after installing the locked Python dependencies, Pi packages and the terminal-emulator dependency in `requirements-ui.txt`:

```sh
/absolute/venv/bin/python -m pip install --target .validation/ui-test-deps -r validation/tui/requirements-ui.txt
PYTHONPATH="$PWD/.validation/ui-test-deps" /absolute/venv/bin/python -B validation/tui/run.py --output /absolute/new-validation-directory
```

This builds a frozen Python deployment, checks types and the reviewed Pi patch, runs the existing native-tool regressions, and exercises the real host, SDK, CLI and PTY with a localhost SSE provider and a deterministic Python evaluator. No external model credentials or NPU profile are used. Original Journals, checkpoints, model requests, PTY output and process logs stay in the selected directory; `acceptance.json` identifies them by SHA256.

Checks run sequentially by default to limit memory pressure. Use `--jobs 3` when sufficient RAM is available. The separate rendered-screen runner accepts `--timeout-seconds 90` for a loaded workstation; this changes the test deadline, not the product's execution policy.

Coverage includes:

- Ordinary codegen: native Pi startup with an idle host and zero model/tool effects, direct project read/edit/bash, large files and symlinks, 35 additional user turns without an implicit limit, `/new`/`/clear`, compaction, interruption, explicit Goal completion, provider failure and resume without replay.
- Missing-model setup remains interactive; an explicit one-credit budget stops before editing and leaves no reservation.
- Durable input deduplication and conflicts; prompt, steering and follow-up boundaries; busy-prompt rejection; interruption and later continuation.
- The same read/edit/evaluate sequence through SDK and native TUI; Chinese multiline paste, resize, long output, user bash, compaction, context reset, exit and attach from a fresh terminal.
- Twenty user turns across worker processes, metered compaction and a source-bound native fork with retained user constraints.
- Worker loss during an accepted evaluation, tool receipt loss from Pi, truncated JSONL, host death, stale socket restart and token rotation. The evaluator executes once.
- Wrong token and widened socket permissions; concurrent worker admission and stale epochs; unstarted cancellation and uncertain reservations.
- Pause, one-exit continue, awaiting-user preservation, background Goal completion, automatic Trial limits, and exhausted credits during initial file creation or model execution.
- Provider failure without retry; a later explicit input uses a new model action identity.
- E1/E2/E3/E4 separation, noise thresholds, source/evaluator identity, branch comparison and task-version changes.

The `lifecycle` scenario uses protocol clients to exercise worker loss and handoff. The separate PTY scenario uses the actual native interface. Synthetic evidence and deterministic local results do not qualify device performance or a platform submission. Linux/macOS cross-host recovery, a real different-uid connection attempt, NPU job acknowledgement loss, Vector/Cube acceptance, and device 20-turn runs remain separate checks.

To exercise the current checkout's actual root launcher after running `source/scripts/develop.py`, use new output directories:

```sh
PYTHONPATH="$PWD/.validation/ui-test-deps" /absolute/venv/bin/python -B validation/tui/codegen_scenario.py \
  --launcher "$PWD/kernelagent" --output /absolute/new-codegen-validation
PYTHONPATH="$PWD/.validation/ui-test-deps" /absolute/venv/bin/python -B validation/tui/codegen_edges.py \
  --launcher "$PWD/kernelagent" --output /absolute/new-codegen-edges-validation
```

The first test asserts the exact model-call count to detect duplicate delivery under consecutive input. Both retain raw PTY output; the main scenario also saves readable screens and every local provider request.

An optional **live, billable DeepSeek** check uses the official `ds` and `ds-pro` configuration from the root README and the private `~/.kernelbridge/credentials.json` file. It removes inherited key/config overrides, edits only a fresh fixture project, and exercises model tools, route selection and host restart. It is not part of the offline test suite:

```sh
PYTHONPATH="$PWD/.validation/ui-test-deps" /absolute/venv/bin/python -B validation/tui/deepseek_live.py \
  --launcher "$PWD/kernelagent" --output /absolute/new-deepseek-validation
```

Individual scenarios can be rerun against a frozen deployment using `product_scenario.py --deployment DIR --output NEW_DIR --scenario sdk|long|recovery|steering|lifecycle|transport|admission|fork`, or `pty_scenario.py` with the same deployment/output options. The admission case pauses between `prepare_turn` and the model call, verifies zero model spending and no unresolved call, then resumes the same worker to full fixture acceptance. Add `--launcher /absolute/installed/kernelagent` to exercise the installed wrapper, and use its `runtime/venv/bin/python` and `--deployment /absolute/installed/runtime`. Choose a new output directory for each run so failed attempts and original receipts are preserved.

For rendered-screen checks, install `requirements-ui.txt` into a separate local dependency directory and run:

```sh
"$BUNDLE/runtime/venv/bin/python" -m pip install --target .validation/ui-test-deps -r validation/tui/requirements-ui.txt
PYTHONPATH="$PWD/.validation/ui-test-deps" "$BUNDLE/runtime/venv/bin/python" -B validation/tui/interaction_scenario.py \
  --deployment "$BUNDLE/runtime" --launcher "$BUNDLE/kernelagent" --output /absolute/new-ui-validation-directory
```

This uses a terminal emulator to check the actual screen after Tab completion, Goal lifecycle commands, history scrolling, rejected commands, and a resize to 48 columns. It preserves the raw PTY stream and six readable screen captures. The Goal policy retains its full original input in the Journal; the TUI displays a short continuation label.

## Operator, subagent and session checks

Complete feature implementation precedes these tests. The dedicated native PTY suite uses the actual Pi and community subagent plugin with a local SSE fixture, real CPU compilation and registered pipeline tools:

```sh
PYTHONPATH="$PWD/.validation/ui-test-deps" /absolute/venv/bin/python -B validation/tui/advanced_scenario.py --output /absolute/new-advanced-run
PYTHONPATH="$PWD/.validation/ui-test-deps" /absolute/venv/bin/python -B validation/tui/subagent_cancel_scenario.py --output /absolute/new-child-cancel-run
node --test validation/tui/session-discovery.mjs
/absolute/venv/bin/python -B -m unittest discover -s validation -p test_subagents.py -v
/absolute/venv/bin/python -B -m unittest discover -s validation -p test_operator_pipeline.py -v
/absolute/venv/bin/python -B -m unittest discover -s validation -p test_operator_infra.py -v
/absolute/venv/bin/python -B -m unittest discover -s validation -p test_business_diagnostics.py -v
```

The infra tests use `KERNELBRIDGE_INFRA_SOURCE` or the existing local ops checkout and otherwise skip. The DQ formula replay additionally requires the retained framework and its optional Python dependencies; synthetic timing inputs do not establish a score. `test_scatter_torch.py` separately requires real CPU `torch` and `torch_scatter`; no-Torch pipeline tests use only a C++ compiler and the Python standard library. Keep optional Torch dependencies separate from the locked host environment.

`operator_profile_smoke.py --config PATH --output NEW_DIR` verifies native idle startup with the explicitly combined 27-tool configuration and local model credentials, without calling the provider or a device. Its output records zero execution. Full device profiling, Ascend compilation and platform scores remain outside local acceptance.
