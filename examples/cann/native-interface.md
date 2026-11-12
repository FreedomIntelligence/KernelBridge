# Native six-source interface

Interface version: `kernelbridge.native-six-source-interface/v2`.
This describes the installed KernelBridge compiler and the clean native trial
admission contract. It supplies interfaces, not an operator implementation.
The task ABI above and `workflow-inputs.json.candidate_abi` supply actual operator
names and arguments; `contract.json` supplies semantics, types and test policy.
Placeholders in this document must be replaced from those declarations.

## The candidate file

`candidate.py` contains exactly six assignments of Python string literals, one
per name below. The outer file is parsed with `ast.literal_eval`, not imported.
A single leading module docstring (a plain string literal before the assignments)
and comments are allowed. No other top-level statements are allowed.
Do not put imports, function/class definitions, `json.dumps(...)`, f-strings,
file reads, extra assignments or Markdown fences in that outer file. Imports and
the `ModelNew` class belong inside the `model_src` string. The five native strings
must be nonempty; all six strings must be present. A literal may span lines using
Python triple quotes or use normal escaped string syntax.

Let `op` be the task's `native_operator`, `name = op + "_custom"`, and `project`
be `name` split at underscores with every part capitalized and concatenated.
These names are distinct from the Python binding name. `job/build` is a fresh
remote build directory owned by the evaluator, not a required local directory.

| Literal field | Destination relative to `job/build` | Responsibility |
| --- | --- | --- |
| `project_json_src` | `<name>.json` | Valid JSON consumed by `msopgen`; operator type and ordered tensor descriptors. |
| `host_tiling_src` | `<project>/op_host/<name>_tiling.h` **or** `<project>/op_kernel/<name>_tiling.h` | Shared host/device tiling declarations and layout. Exactly one generated header location is replaced. |
| `host_operator_src` | `<project>/op_host/<name>.cpp` | Operator registration, shape/type inference, tiling callback, launch/workspace metadata. |
| `kernel_src` | `<project>/op_kernel/<name>.cpp` | Native device entry point and computation implementing the task. |
| `python_bind_src` | `CppExtension/csrc/op.cpp` | C++ Python extension binding: input/output plumbing and dispatch to the custom native operator. |
| `model_src` | Loaded into the job-local Python runtime after native build | Stateless `torch.nn.Module` subclass `ModelNew`, calling the declared binding. |

The evaluator copies a fixed `CppExtension` template before replacing `op.cpp`.
Its `setup.py` builds an NPU extension named **`custom_ops_lib`**. The installed
helper header **`pytorch_npu_helper.hpp`** is already in `CppExtension/csrc`; it
need not be supplied as a seventh field. The build does not copy arbitrary
candidate-side files or install Python packages from `candidate.py`.

## Project JSON and consistent symbols

The JSON root is an array containing the custom operator declaration. This is
placeholder schema notation, not a complete task-specific declaration:

```json
[
  {
    "op": "<OperatorType from task ABI>",
    "language": "cpp",
    "input_desc": [
      {"name": "<input name>", "param_type": "required", "format": ["ND"], "type": ["<CANN dtype>"]}
    ],
    "output_desc": [
      {"name": "<output name>", "param_type": "required", "format": ["ND"], "type": ["<CANN dtype>"]}
    ]
  }
]
```

There is one descriptor per tensor, in the declared argument order. The single
entries above denote descriptor structure, not a fixed input/output count.
`format` and `type` are arrays, not scalar strings. Use the task's actual types;
common CANN spellings include `float` for FP32, `float16` for FP16, and `int64`
for a signed 64-bit tensor. A generic `inputs`/`outputs` object or a `dtype`
property does not replace `input_desc`/`output_desc` and `type`.

Keep these connections consistent:

| Surface | Required relationship |
| --- | --- |
| JSON `op` | The task's `operator_type`; the same type registered by the host `OpDef` class and `OP_ADD`. |
| Generated source basename | `<native_operator>_custom`, from compiler settings; the JSON must generate those expected files. |
| Native kernel symbol | `<native_operator>_custom`, matching the generated kernel entry contract. |
| ACLNN custom symbol | The custom operator's generated `aclnn<OperatorType>` entry and its `GetWorkspaceSize` companion. |
| Python extension | Module name `custom_ops_lib`; expose the method named by `candidate_abi.binding`. |
| Python model | `ModelNew.forward` calls that exact method with its two unchanged input tensors, in task order. |

Do not infer the Python method name from the kernel symbol: they need not match.
Do not use an existing framework operator with a similar name in place of the
newly generated custom operator.

## Host, tiling and device entry interfaces

Host registration uses `register/op_def_registry.h`, `ops::OpDef`, and
`OP_ADD(<OperatorType>)`. `OP_ADD(T)` constructs `T(const char* name)`; declare
that constructor and forward `name` to `ops::OpDef(name)`. Parameter-kind
constants belong to the `ops` namespace (for example `ops::REQUIRED`). These
are registration interfaces, not an operator implementation.
Register the input/output names, CANN data types and
formats consistently with the JSON and task. The relevant callback signatures
are declarations only:

```cpp
ge::graphStatus TilingFunc(gert::TilingContext* context);
ge::graphStatus InferShape(gert::InferShapeContext* context);
ge::graphStatus InferDataType(gert::InferDataTypeContext* context);
```

`SetInferShape` and `SetInferDataType` connect the inference callbacks;
`AICore().SetTiling(...)` connects the tiling callback. The tiling callback is
responsible for valid tiling data, launch block count and requested workspace
sizes. The device side must interpret exactly the same tiling layout. These
interfaces do not select an algorithm, block count, tile size or memory layout.

`msopgen` versions place the generated tiling header in either `op_host` or
`op_kernel`. The compiler requires exactly one generated `<name>_tiling.h` and
replaces only that file. Its public filename is `<name>_tiling.h`; no duplicate
tiling definition is synthesized. Host and device must agree on the serialized
layout, but that does not make host registration declarations device-safe.
Only include this header in a device translation unit if its device-visible
contents and dependencies are supported by the target compiler. Separate or
conditionally expose host registration and device layout declarations as required
by the actual SDK; the harness does not insert those guards for a candidate.

The generated-include contract `kernelbridge.generated-includes/v1` prepends
absolute project, `op_host`, and `op_kernel` roots to **each build subprocess's**
`CPATH`. Activated-profile SDK/Python include paths remain after those roots;
empty inherited entries are removed so they cannot implicitly include the
working directory. The parent environment is unchanged. Device CMake recipes
also receive explicit `-I` options for the same roots. This lets nested host
code-generation commands inherit the roots even when they do not consume
CMake target include options. Build logs record the added roots and a digest
of inherited paths, not the full environment. An SDK wrapper which clears
`CPATH` requires separate toolchain qualification; this contract does not
silently rewrite candidate includes or modify the installed SDK.

Host register-based tiling declarations use `register/tilingdata_base.h`, which
can transitively import GE host types. Unconditionally importing those host types
after `kernel_operator.h` can conflict with device compilation. Raw layouts
must remain compatible on both sides, including field sizes, order and alignment;
device reads must preserve the SDK's GM address-space qualifiers and supported
tiling access conventions. The compiler does not serialize or repair
user tiling data. Filename/layout ambiguity and actual compiler errors remain
errors, rather than reasons to guess a different declaration.

For the audited CANN 9.2 registration interface, both `BEGIN_TILING_DATA_DEF`
and `REGISTER_TILING_DATA_CLASS` expand names from `optiling`; keep the class
definition and registration in that namespace. Declared fields have generated
`set_<field>` / `get_<field>` accessors, not public plain-struct members. A
registered host serialization class and a raw device byte layout are different
interfaces; do not interchange them merely because their field names agree.
Consult the provisioned SDK's declaration and serialization contract before
choosing either interface.

In this SDK, `gert::TilingContext::GetTilingData<T>()` can return null when raw
storage is absent or its capacity is smaller than `sizeof(T)`. On success it
already sets the data size. A null pointer therefore does not by itself prove
which condition failed, and adding another size call does not establish a fix.
Inspect the original build diagnostics and host callback result before changing
device computation. Successful compiler exit and an SDK error diagnostic can
coexist; neither an empty generated header nor an error-looking line alone is
a new correctness verdict.

These API observations come from the actual provisioned CANN 9.2 headers:
`register/tilingdata_base.h` (SHA256
`6cf04b93bde7081d918b45ccb1a6f4764f3a5f88987bb99fe02bc6e42191d34d`) and
`exe_graph/runtime/tiling_context.h` (SHA256
`25bc84de373531cc66e10da7c673b3076c391add4fef6571afcd97219e5746d2`).
Other SDK versions require checking their own declarations. These notes supply
API boundaries, not a candidate, algorithm, launch configuration or tiling fields.

The kernel entry has C linkage and the Ascend C qualifiers. Its address argument
order follows the generated tensor descriptors, followed by workspace and tiling
addresses. The following is only signature notation, not compilable candidate
source or a function body:

```text
extern "C" __global__ __aicore__ void <native_operator>_custom(
    GM_ADDR <each input in order>, GM_ADDR <each output in order>,
    GM_ADDR workspace, GM_ADDR tiling);
```

Device API declarations come from the provisioned SDK, commonly through
`kernel_operator.h`. SDK API availability is determined by the frozen remote
profile/toolchain; the local workspace is not promised to contain SDK headers.
The compiler preserves full original build logs, so missing symbols and include
errors are reported by the actual toolchain rather than inferred from a template.

## C++ binding and Python wrapper

The C++ extension uses `<torch/extension.h>` and the installed
`"pytorch_npu_helper.hpp"`. A binding function accepts the task's Tensor arguments
and returns its output Tensor. Export it through `PYBIND11_MODULE(TORCH_EXTENSION_NAME, m)`
under the method name declared in the task ABI. The extension module name remains
`custom_ops_lib`, independent of the C++ function name.

The helper's interface is `EXEC_NPU_CMD(<custom ACLNN symbol>, <tensor arguments
in generated API order>)`. It resolves the custom symbol and its workspace-size
entry, converts arguments, owns the converted parameters and workspace, and
dispatches on the current NPU stream. Supply device tensors and the required
output storage; do not substitute host computation or hardcode a different
device. Output allocation/shape and returned values must obey the task contract.
The helper is infrastructure, not an operator implementation.

The clean-trial model audit accepts `import torch`, `import custom_ops_lib`, and
one class `ModelNew(torch.nn.Module)`. Its `forward(self, <input0>, <input1>)`
contains a single direct return of the declared `custom_ops_lib` method with those
two input names. An inherited constructor or a constructor containing only
`super().__init__()` is accepted. `torch.Tensor`, string and `None` annotations
are accepted; executable annotations, argument defaults, decorators, auxiliary
methods and module-level execution are not. Tensor computation belongs in the
native implementation, not the Python wrapper or a framework fallback in the
C++ binding. These restrictions are the existing admission policy, not a new
algorithm recommendation.

## What the evaluator executes

The compiler entry is `compile_sources(sources, BuildSettings(operator,
workspace, soc, device), ...)`. `operator` is a single identifier, `workspace`
is absolute, `soc` is the exact `ai_core-Ascend...` target supplied by the profile,
and `device` is a nonnegative index. The evaluator invokes, in order:

1. `msopgen gen -i <name>.json -c <soc> -lan cpp -out <project>`.
2. Replace the generated host header, host source, kernel source and extension
   binding with the corresponding literal strings.
3. Run the generated project's `./build.sh`.
4. Install its sole `custom_opp_*.run` into the job's own `opp` directory.
5. Run the current interpreter's `setup.py build_ext --inplace` in `CppExtension`.
6. Verify installed output hashes and target, select the bound NPU, load the
   job-local `custom_ops_lib`, and instantiate the supplied `ModelNew`.

Host `AddConfig("ascend910...")` strings are normalized to the supplied SoC
family by the compiler. It does not repair operator names, JSON schemas, shape
inference or kernel logic. Existing project directories are rejected rather than
rebuilt under the same attempt identity. The ordinary six-source session supports
Python execution of an outer file, but these clean benchmark drivers deliberately
use the stricter six-literal parser described above.

For this task the configured pipeline owns compilation and measurement. The
tool IDs and ABI are in `workflow-inputs.json`; no extra local compile script is
required. This document changes neither the suite nor measurement policy, turn
budget, scheduling or recovery rules. A source bundle or successful compilation
alone is not correctness or speedup evidence.

## Source identity of this interface description

Reviewed against `kernelcat-executor` version `0.8.0`, checkout base
`3086d182798a35571be31bf7f3f96375a77ac2f2` plus the following exact working-tree
sources. The hashes, not the base commit alone, identify this reviewed interface.
Paths are repository-relative provenance; they do not imply those files or SDK
headers are available in the clean agent workspace. Re-review this description
if one of these interfaces changes.

| Source | SHA256 |
| --- | --- |
| `source/packages/executor/pyproject.toml` | `e170fa4cb2cac1e9f586d8e61cebf20c6b77cc7a23a62ced6891e4a7d53d5744` |
| `source/packages/executor/src/kernelcat_executor/backends/ascend/compiler.py` | `47c0193c17f9fbdeca72429f613acb38cdc8a1a3b1c1890db501546e4c28df51` |
| `source/packages/executor/src/kernelcat_executor/build/inputs.py` | `8900bae54a0cc4ec86930c05a2dec649771e8aced363ef932a106b42b2a16b8b` |
| `source/packages/executor/src/kernelcat_executor/backends/ascend/runtime.py` | `1455242ec7d7dcb0d489dbbce292d5b0a1c7fbcb057b57754e9b77587982fbfb` |
| `source/packages/executor/src/kernelcat_executor/evaluation/protocols/six_source/audit.py` | `15f1a1d79029a57ec577a5a605ceb230b254e7f9b3eafdceb78a41f34484babc` |
| `source/packages/executor/src/kernelcat_executor/backends/ascend/templates/CppExtension/setup.py` | `318695abf92971c6adfe8aa64941afc4e1899a855df5e5328408452607f87355` |
| `source/packages/executor/src/kernelcat_executor/backends/ascend/templates/CppExtension/csrc/pytorch_npu_helper.hpp` | `b092bd7c36b6aa38f4abc817d7fe939fa98c45ba71b51dcfb26989830b468968` |
| `examples/scatter/npu_candidate_audit.py` | `d2820c66ca61bac88bbb83d87e8961bc8dc0b69262744df33419da33a8c63ed1` |
| `examples/scatter/npu_benchmark.py` | `059b5534feaf431ac7528f11585442c9a67223098aba047d823ce25d75259c18` |
| `examples/scatter/npu_matmul_benchmark.py` | `4e83711f0f9315efdfa70f3514862559f071ed735663489055d8cfce674007c5` |
| `examples/operator_pipeline/preflight.py` | `e26255219c08851c3dffb6b91fe57dfe4c82e21fb6cfe4d8421932c61616b564` |
