# Auxiliary clean-start task: native Cube matmul

The auxiliary task is `examples/cann/cube-matmul`. Cube data movement, matrix
tiling, FP32 accumulation, workspace ownership and kernel launch differ from the
scatter tasks and from vector addition. This checks whether improvements to the
harness transfer to a different execution unit and operator structure. It remains
one public shape with five fixed seeds; it does not establish general GEMM or
external platform qualification.

## Frozen task and comparison

The original contract is FP16 contiguous ND A `[128,128]` and B `[128,128]`,
FP32 accumulation, FP16 C `[128,128]`, no transpose/bias/batch, and no input
mutation. Inputs use sequential CPU `torch.randn` calls from one generator per
seed, seeds 1024–1028. The independent oracle multiplies the already rounded FP16
inputs in CPU FP32 and rounds its output to FP16. Both candidate and eager must
pass every seed with the original `atol=0.01, rtol=0.01`. Oracle execution and
input transfer are outside timing.

The timed baseline is `examples/scatter/npu_matmul_eager.py`: `torch.matmul` with
both inputs and output on the selected NPU. The original `reference.py` transfers
to CPU and is only an oracle; timing that function as NPU eager is invalid.

The candidate is six literal source strings, parsed without executing the outer
file: `project_json_src`, `host_tiling_src`, `host_operator_src`, `kernel_src`,
`python_bind_src`, `model_src`. The ABI is native `MatmulCustom`, kernel entry
`matmul_custom`, and stateless `ModelNew.forward(self, a, b)` returning
`custom_ops_lib.matmul_custom(a, b)` directly. An empty constructor calling
`super().__init__()` or the inherited `torch.nn.Module` constructor is accepted.
The existing six-source model audit is reused. Additional source checks reject
Torch/ACLNN matmul delegation, CPU computation, scalar tensor simulation and
module-level Python execution. A static source screen does not by itself prove
which hardware instructions executed: the original source, build outputs and
source-bound diagnostic artifacts remain part of the evidence.

`npu_candidate_audit.py` shares the stateless wrapper and native binding checks
with scatter. The binding name is parameterized, and normalization for the
existing audit happens only on an AST copy. Source bytes are never rewritten.
Ordinary `torch.Tensor` and string annotations are accepted; executable default
arguments and annotation expressions are rejected.

Original contract identities, inspected before the first formal trial:

| File | SHA256 |
| --- | --- |
| `examples/cann/cube-matmul/task.json` | `375041cfd1a014dc294bf0d080711fb9aa33079d136a344c917621c58e041b2d` |
| `examples/cann/cube-matmul/case.json` | `d8dee72bfb882a6a5706fe39e075cf94572dd95507f4018878ff6485bd839d89` |
| `examples/cann/cube-matmul/reference.py` | `5d00d7b2e9c892015fa0856a4c26ffcf64817838533a300c7b7c67c7bdbe59b7` |

## F and G are distinct measurements

F retains all five correctness trials and the original warmup 3 plus 100 NPU Event
samples. Its timed input is seed 1024, matching the original input generator after
five correctness calls. Every raw sample is retained in `F.json`; no samples are
discarded. Host wall-clock timing is not substituted for NPU Events.

The historical `validation/native_rsi_adapter.py` uses twelve **alternating**
two-arm pairs, not ABBA. It is unchanged and is not described as ABBA evidence.
The new Goal explicitly requires eager ABBA, so its sole G qualification protocol
is independently named `cube-fp16-128-eager-abba-12x20-v1`, frozen before any
formal trial: twelve complete eager/candidate/candidate/eager blocks, warmup 3
and twenty NPU Event samples in each arm. Each block uses seed
`1024 + block % 5` for all four arms. All five correctness seeds are rechecked
before each arm. One measurement case contains the five correctness subchecks;
seeds are not promoted into five new measurement cases. A block's log gain is
`log(mean(A0,A3) / mean(B1,B2))`, where each arm is the mean of its twenty
raw Event samples. The reported gain is the exponential of the mean block log
gains (`aggregation=paired_block_log_mean`).

An independent eager/eager four-arm calibration runs in the same original job
before candidate G. Its sample counts, warmup, device and seed schedule are the
same as G. The noise threshold uses the existing formula
`abs(mean(null_block_log_gain)) + 3 * stdev(null_block_log_gain) / sqrt(blocks)`.
The candidate's own outer eager endpoints are not an independent noise sample.
`noise.json` is written before candidate G and its exact file hash is recorded;
`noise_calibration` also preserves all raw samples in the final result. The
pipeline qualification layer independently recomputes the noise threshold.
Calibration never changes the eager implementation. No historical-versus-new
protocol selection or outlier selection is allowed after seeing results.

The driver refuses non-frozen `--warmup`, `--repeats` or `--pairs` values.
It records all arm samples, input hashes, correctness subchecks, evaluator and
baseline hashes, effective device/environment, source digest and ELF/build
hashes. The pipeline adapter consumes these original records; F/G/H views do not
launch another device job. Regression or failure triggers the existing diagnostic
policy against this job. Compiler inability to emit IR and missing profiler
artifacts are reported as unavailable with original attempt evidence, not as a
successful diagnostic or a reason to rerun a broken kernel.

## Clean trial inputs and evidence boundary

The trial starts in an empty workspace with the clean task and NPU profile and a
new application session. Initial trials used `application.max_model_turns=20`;
subsequent trials follow the user's removal of total turn/token limits and leave
that setting unset, recording actual parent, child and compaction usage. The task
declares the ABI and frozen measurement contract, not an implementation. Its oracle and eager
files are pinned evaluation resources. `baseline.py`, `native/`, old candidates,
old sessions and `task.compiled.json` are excluded: the compiled task embeds the
old complete solution under `reference/native-template.py`. The trial runner
must not seed or copy that file. Repository-wide reads exposing those solutions
must be prevented or reported as clean-start contamination.

The native compiler and installed SDK are tools the agent may inspect; there is
no operator implementation in the harness prompt, profile or new driver. All
device jobs remain serialized by the shared profile. A timeout is recovered from
the original handle, never resubmitted. Device, SDK and Torch versions are read
from the actual run; older qualification environments are not reused as proof.

This document specifies the task and measurement interface. A successful local
interface test is not a device result. Only actual original receipts can establish
E1 correctness, E3 above-noise speedup and promotion. There is no E4 platform claim.

## Local interface validation

`python3 -B -m unittest validation.test_npu_matmul_benchmark -v` passed 11 tests.
The checks cover original five-seed input/oracle equality on CPU, exact Event
counts for F and G, independent null arm ordering, seed rotation, partial-sample
checkpoints, invalid duration/config rejection, source parsing without execution,
shared wrapper audits and exact calibration receipt serialization. CLI `--help`
also works without importing an NPU runtime. These tests generated no native
candidate and dispatched no device job. The detailed local log is
`.validation/npu-matmul-interface-r2.log`.
