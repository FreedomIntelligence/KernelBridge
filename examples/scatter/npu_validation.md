# Native scatter comparison

`npu_trial.py` is the single-candidate business tool used by the NPU campaign.
It freezes a candidate, launches one native attempt, and collects that original
attempt. The parent coordinator supplies `requested.json` with `round` and
`input_id`; stdin contains the Action `id` and `arguments: {round, hypothesis}`.

```json
{
  "candidate": "/project/candidate.py",
  "initial_candidate": "/project/initial.py",
  "state": "/project/trials",
  "transport_module": "/local/authorized_transport.py",
  "remote_root": "/remote/task",
  "attempt_namespace": "rounds",
  "mode": "max",
  "device": 0,
  "warmup": 3,
  "repeats": 10,
  "pairs": 3,
  "profile_rounds": [1, 20]
}
```

The transport exports `run(argv, stdin=bytes, timeout=seconds)` returning
`returncode`, `stdout`, and `stderr`. Provision the remote `profile.json`, the
executor sources and `tools/npu_*.py` plus `tools/diagnostic_policy.py` first.
`npu_enter.py` activates only that profile's SDK/Python and optional Python
development headers. `ascend_sources.candidate(mode, 33)` supplies an initial
six-source candidate. The campaign tool requires a native implementation change;
comments, formatting, metadata and Python wrapper changes alone do not count.

```sh
python3 -B examples/scatter/npu_trial.py trial-config.json < action.json
python3 -B examples/scatter/npu_trial.py trial-config.json --recover 7
```

Recovery never launches another process. Lost launch acknowledgement or failed
collection returns `unknown`; a prior unresolved attempt blocks the next round.
Every completed receipt binds candidate, baseline and driver hashes to the
original process handle, input hashes and individual timing arms.

The measured contract is finite FP32 `src[N,F]`, int64 `index[N]`, dimension 0,
33 output groups, values only, with empty groups equal to zero. Six deterministic
cases cover sparse groups, collisions, sorted indices, a hot subset and uneven
feature widths. The independent CPU oracle checks exact values outside timing.

The baseline is **Torch NPU eager broadcast-mask reduction**, implemented in
`npu_eager.py`; it is not the `torch_scatter` extension. It expands a mask and uses
NPU `where`, `amax`/`amin`, and `any`, with O(B*N*F) intermediate storage. On the
validated Torch 2.9/torch_npu 2.9 environment, `torch_scatter` is absent and
`scatter_reduce_` max/min falls back to CPU, so that unsupported path is excluded.
Profiler evidence verified the eager composition's real NPU kernels. Comparisons
must retain this baseline name rather than claim a native torch_scatter result.

Each case uses eager/candidate/candidate/eager arms on the same NPU in the same
process. Timing includes dispatch and output allocation, with synchronization at
each arm boundary; it is not isolated device-kernel time. Across-round results
are best observed results, not paired incumbent promotion or official scores.

Failure, a case regression, or an explicit profile round invokes the shared
diagnostic policy. A correct kernel is profiled after timing; compile/correctness
failures do not execute a broken kernel for profiling. IR diagnostics reuse the
original CANN make recipe and pin compiler hash, arguments, PID and logs. The
validated CANN 9.2 compiler rejects LLVM emission; its `unavailable` report and
unsupported-option log are retained. An object file, preprocessed C++ file or
empty marker never counts as IR. Diagnostic completeness remains separate from
correctness and performance acceptance.
