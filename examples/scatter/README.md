# Feature-index scatter max/min

The clean trial implements finite FP32 `src[N,F]`, int64 `index[N]`, dim-0
reduction into 33 output groups, values only, and zero for empty groups. This
bounded contract does not include the complete `torch_scatter` API or argmax.
The existing `ascend_sources.py` is an example implementation and is deliberately
not supplied to, or readable by, a clean optimization trial.

`npu_eager.py` is the frozen full-NPU broadcast-mask plus `amax`/`amin` comparator.
The independent CPU oracle checks all six cases and is excluded from timing.
CPU fallback does not qualify as an NPU baseline.

Deploy the pinned `npu_*.py` helpers and `../operator_pipeline/process_isolation.py`,
`diagnostic_policy.py`, and `preflight.py` to the authorized remote profile's
`tools/` directory. Initialize its PID-view profile before creating a trial:

```sh
python3 -B examples/scatter/prepare_npu_isolation.py initialize \
  --profile /path/to/npu-profile.json \
  --transport /path/to/authorized_transport.py \
  --remote-root /path/to/remote/campaign --device 0 \
  --output /path/to/new-isolation-profile
python3 -B examples/scatter/prepare_npu_campaign.py \
  --profile /path/to/new-isolation-profile/initialized-profile.json \
  --output /path/to/trial-max --mode max --device 0 \
  --transport /path/to/authorized_transport.py \
  --remote-root /path/to/remote/campaign --python /path/to/python \
  --runtime-profile /path/to/runtime-profile.json
./kernelagent tui --task /path/to/trial-max/task.json \
  --profile /path/to/trial-max/profile.json --output /path/to/new-run \
  --goal-from-task --on-detach continue
```

Initialization creates its own directory under `.infrastructure-probes/` inside
the existing authorized remote root. It records a bounded context probe and
original PID-view evidence, without building a candidate, running the operator
suite, or establishing performance. Pin extra local transport imports with
repeatable `--transport-dependency PATH` when needed. The returned
`initialized-profile.json` carries the required isolation policy.

Initialization waits for its original receipt for up to 60 seconds by default;
this is not an operator or model budget. If its outcome is unknown, recover the
same saved probe without repeating initialization or dispatch:

```sh
python3 -B /path/to/new-isolation-profile/tools/prepare_npu_isolation.py recover \
  --output /path/to/new-isolation-profile
```

The campaign can be prepared only after initialization has returned a valid
profile. Preparation rejects
missing or invalid view evidence before creating a trial; the model cannot
supply a claimed proof or disable this requirement through `pipeline_bind`.
Every native dispatch still checks the activated SDK environment, physical-chip
mapping, cooperative shared lease, and current process observations. Historical
initialization evidence is not proof that the device is idle now. Existing prepared
profiles and retained receipts continue to use their original contracts; this
preparation requirement applies to newly created campaigns.

The runtime profile declares `python`, `node`, `expected_python_modules`, model
providers and policy under `config`. Use credential environment names; private
credentials stay outside task/profile files. Preparation seeds only readonly
contract, workflow and ABI documents. The model must create `candidate.py`.
Use independent output directories and `--mode min` for min.

An unset budget imposes no total model-call, token, or context-session ceiling.
Explicit runtime budgets are preserved; `--max-model-turns N` sets a model-call
limit only when supplied. Usage still counts parent, child, compaction, and review
calls and tokens. A Goal Trial is a context session, not one native device
attempt or optimization round. The harness receives all A-J tools and chooses
the steps itself. E creates one original native job;
F/G/J project that job's full-suite checks, original ABBA samples and diagnostics.
Jobs cooperate through a same-UID shared lease for the verified physical device;
this and sampled observations do not prove absolute device exclusivity. A verified
pre-dispatch `not_started` receipt permits an explicit new action. A dispatched
unknown outcome retains its original handle and requires recovery, never a
replacement launch. Foreign occupancy or incomplete observation preserves the
original correctness and timing records while preventing G/H performance
qualification. A settled nonqualifying isolation result may be explicitly
remeasured with the same source; every earlier attempt stays in the journal.

Scatter retains three ABBA blocks and the original pooled-six median speedup.
Separate eager/eager AAAA blocks calibrate noise using the same device, inputs
and measurement settings. H requires full F correctness, GM greater than one,
and paired log improvement above the calibrated threshold. Every regressing
case/family remains in the receipt. E4 platform acceptance is separate. Without
a configured performance baseline, correctness is F-only evidence, not an eager
speedup or E3 performance acceptance.

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
