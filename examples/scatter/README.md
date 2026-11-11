# Feature-index scatter max/min

The clean trial implements finite FP32 `src[N,F]`, int64 `index[N]`, dim-0
reduction into 33 output groups, values only, and zero for empty groups. This
bounded contract does not include the complete `torch_scatter` API or argmax.
The existing `ascend_sources.py` is an example implementation and is deliberately
not supplied to, or readable by, a clean optimization trial.

`npu_eager.py` is the frozen full-NPU broadcast-mask plus `amax`/`amin` comparator.
The independent CPU oracle checks all six cases and is excluded from timing.
CPU fallback does not qualify as an NPU baseline.

Prepare an empty trial after deploying the pinned `npu_*.py` helpers and
`../operator_pipeline/diagnostic_policy.py` to the authorized remote profile:

```sh
python3 -B examples/scatter/prepare_npu_campaign.py \
  --output /path/to/trial-max --mode max --device 0 \
  --transport /path/to/authorized_transport.py \
  --remote-root /path/to/remote/campaign --python /path/to/python \
  --runtime-profile /path/to/runtime-profile.json
./kernelagent tui --task /path/to/trial-max/task.json \
  --profile /path/to/trial-max/profile.json --output /path/to/new-run \
  --goal-from-task --on-detach continue
```

The runtime profile declares `python`, `node`, `expected_python_modules`, model
providers and policy under `config`. Use credential environment names; private
credentials stay outside task/profile files. Preparation seeds only readonly
contract, workflow and ABI documents. The model must create `candidate.py`.
Use independent output directories and `--mode min` for min.

One trial is capped at **20 actual model calls**, including compaction, children
and review. Child calls and tokens are reported separately within that total.
It is not twenty externally prompted candidate edits. The harness receives all
A-J tools and chooses the steps itself. E creates one original native job;
F/G/J project that job's full-suite checks, original ABBA samples and diagnostics.
Only one device job executes at a time. Unknown outcomes retain the original
handle and require recovery, never a replacement launch.

Scatter retains three ABBA blocks and the original pooled-six median speedup.
Separate eager/eager AAAA blocks calibrate noise using the same device, inputs
and measurement settings. H requires full F correctness, GM greater than one,
and paired log improvement above the calibrated threshold. Every regressing
case/family remains in the receipt. E4 platform acceptance is separate.

An optional read-only observer launches exactly that CLI and records the original
Journal without steering the model or writing a candidate:

```sh
python3 -B examples/scatter/run_operator_trial.py \
  --task /path/to/trial-max/task.json --profile /path/to/trial-max/profile.json \
  --output /path/to/new-run
python3 -B source/scripts/audit_operator_trial.py /path/to/new-run \
  --output /path/to/audit.json
```

The audit separates automatic diagnostic capture, explicit J calls and artifact
reads. IR export failure is reported as unavailable. Tool availability alone is
not evidence that the agent called, read or used it. An observer timeout preserves
the original session and does not authorize another job.
