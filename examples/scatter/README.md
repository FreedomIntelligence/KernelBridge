# Feature-index scatter max/min

`ascend_sources.py` generates native Ascend C max/min implementations. The device
campaign uses finite FP32 `src[N,F]`, int64 `index[N]`, reduction along dimension 0,
33 output groups, values-only output, and zero for empty groups. This is a bounded
contract, not the complete `torch_scatter` API (for example, it does not return argmax).

`npu_eager.py` provides a Torch NPU eager composition using a broadcast group mask
and `amax`/`amin`. A backend which silently sends `scatter_reduce` to the CPU is
not an NPU baseline. The driver checks both candidate and eager outputs against
an independent CPU per-group oracle before timing. Oracle work is outside timing.

Prepare each operator separately after deploying the `npu_*.py` helpers
and `../operator_pipeline/diagnostic_policy.py` to the remote
profile's tools directory:

```sh
python3 -B examples/scatter/prepare_npu_campaign.py \
  --output /path/to/campaign-max --mode max --device 0 \
  --transport /path/to/authorized_transport.py \
  --remote-root /path/to/remote/campaign --python /path/to/python
node examples/scatter/run_npu_campaign.mjs \
  --campaign /path/to/campaign-max --config /path/to/model-config.json
```

Use a separate output directory and `--mode min` for min. The transport module
exports `run(argv, cwd=None, stdin=b'', timeout=...)`, returning `returncode` and
byte strings `stdout`/`stderr`. Its credentials stay outside tracked files.
The remote profile provides the Python environment, CANN environment and frozen
executor deployment expected by `npu_enter.py`.

The runner makes 20 model-led optimization attempts per operator. Each requested
round edits the six-source `candidate.py`, freezes the source, and calls `npu_trial`
once. Compilation and correctness failures remain failed rounds. Unknown dispatch
outcomes are unresolved and cannot be replaced with a new launch. Recover the
original handle, then resume the same campaign:

```sh
/path/to/python -B examples/scatter/npu_trial.py \
  /path/to/campaign-max/trial-config.json --recover 7
```

Each correct candidate is measured on the same device in three ABBA groups:
eager, candidate, candidate, eager. Both sides include output allocation. Timing
and source hashes, original process handles, driver/baseline identities and
diagnostic artifacts are retained. Failure or a nonpositive pair invokes the
shared diagnostic policy; configured profile rounds also collect trace evidence.
Unavailable IR or profiling is reported explicitly.

`progress.json` records all original results. `best-observed.py` is the fastest
observed correct candidate versus eager. Its ranking across rounds is not a
paired comparison against the preceding incumbent and is not an official score.
The runner continues from the best observed correct source while keeping failed
variants. No implicit Goal, Trial budget, or token budget is created.
