# KernelCat 910C Agent-Native Control Plane

## 0. 改造前快照

改造基线已经以不可变 Git subtree 完整保存：

- source commit：`85de5ff6a8c8a4df0fe0640bff6f9e0da42d805a`
- source tree：`f9ff99083efa457ccac5df8ddec034f910f61011`
- backup：`backups/cann_910c_pipeline_pre_refactor_20260826/cann_910c_pipeline/`
- manifest：`backups/cann_910c_pipeline_pre_refactor_20260826/BACKUP_MANIFEST.json`

备份目录直接引用原始 tree，因此 tracked path、file mode、blob SHA 与嵌套 tree 均相同：

```bash
python cann_910c_pipeline/kcatctl.py --pretty verify-backup --repo-root .
```

## 1. 改造目标

旧脚本各自解决了真实问题，但跨脚本缺共同事实模型：parent 含义不唯一、候选身份依赖目录、profiler/full20 重复编译、`runs=[]` 容易伪装成中性结果、PMU 与榜面证据混杂、4-op carrier 增量易重复归因、并发与恢复语义分散、Agent 获得大日志却缺少小而充分的裁决视图。

新控制面把现役执行器放进一条可验证证据链，而不是一次性重写稳定的远端协议：

```text
Environment + ParentSet + one Policy + Source/Task/Casepack
                         │
                  immutable Candidate
                         │
             base/candidate BuildIdentity
                         │
                  ExperimentPlan
                         │
       Host/Compile/Correctness/Event/PMU/Formal
                         │
                    GateReceipt
                         │
               Official + ScoreSnapshot
                         │
             component-level Attribution
```

## 2. 领域模型

### `EnvironmentFingerprint`

冻结 SoC、compute unit、ISA、CANN、task version、toolchain hash 和 casepack hash。不同环境的证据不可混用。

### `ParentSet`

- `code_parent`：本次 patch 从哪个已验证源码产生。
- `decision_parent`：晋级时必须严格打败的 current parent。
- `carrier_parent`：多算子 bundle 的载体父本，可选。

这避免“从旧父本改出来却和错误榜面父本比较”。

### `Policy`

一个 Policy 只编码一个机制：假设、适用域、fallback、预期 profiler 信号、触及表面和真源引用。多个机制必须拆成多个 candidate/arm，才能做归因和失败墓碑。

### `Candidate`

Candidate 冻结 source tree SHA-256、CAS manifest、ParentSet、Policy、task/casepack/environment、capability predicate 和 fallback entry。时间戳、本机盘符与工作目录不进入身份。

### `BuildIdentity` / `ExperimentPlan`

构建身份由 source、task、environment 与 build options 决定。Experiment 冻结 backend、cases、repeat、device pool、metric groups 和 seed，并生成幂等 request key。

### `EvidenceRecord`

每份证据都有 kind、status、candidate/experiment、source/casepack、payload、artifact refs 与 provenance。缺原始 artifact ref 会被拒绝。

## 3. 存储与恢复

### CAS

`.kernelcat/910c/cas/objects/sha256/` 保存源码 manifest、stdout/stderr、CANNBench JSON/CSV、Event 样本、PMU 原始 counter、unified feedback、gate receipt、score snapshot 与迁移原文件。对象原子落盘，读时重新校验 SHA-256。

### SQLite Ledger

`.kernelcat/910c/ledger.sqlite3` 使用 WAL、foreign keys、busy timeout 与 `BEGIN IMMEDIATE`：

- `entities`：不可变 Parent/Policy/Candidate/Experiment/Evidence/Gate；
- `events`：追加式事实；
- `runs`：可恢复调度投影；
- `leases`：设备、workspace、slot、账号的 owner/token/TTL；
- `artifacts`：CAS 索引。

同一实体 ID 写入不同 payload、同一 idempotency key 写入不同 request、非法 run 状态迁移都会失败。

### 明确失败语义

以下全部是失败，不是“零收益”：

- return code 非零或 timeout；
- required artifact 缺失；
- required JSON 字段缺失/为空；
- profiler 缺 `UNIFIED_FEEDBACK` 终态 marker；
- A/B `runs=[]`；
- lease owner/token 不匹配；
- remote pending 未恢复到终态。

stdout/stderr 无论成功或失败都会写 CAS。

## 4. Compile once, evaluate twice

`UnifiedEvaluationPlan` 固定 base 与 candidate 两个 `CompiledArm`：

1. base 编译一次；
2. candidate 编译一次；
3. profiler-off Event 复用这两份产物；
4. directed PMU 仍复用这两份产物；
5. correctness/final JSON 独立保留；
6. 统一 feedback 分为 compact decision view 与完整 evidence catalog。

Profiler 不再为了证据包重复 setup/compile 两臂。

## 5. Event 与 PMU 的硬边界

### Event A/B：性能裁决

要求 `profiler_enabled=false`、same device、correctness 先通过、每臂足够样本、稳定尾部、bootstrap 95% LCB > 1、full20 aggregate delta > 0，并绑定 build/source/casepack。

### PMU：机制解释

PMU 视图排除 Duration/elapsed 字段，counter 可按 duration 加权，并对照 Policy 的 expected direction。输出只保留少量高信息信号；`decision_eligible=false` 是硬语义。PMU 再漂亮也不能弥补 Event 或 official 失败。

## 6. 强晋级门

`GateReceipt` 检查：

1. current decision parent 未移动；
2. host proof；
3. compile；
4. representative exact correctness；
5. fallback exact correctness；
6. same-card full20 profiler-off Event A/B；
7. LCB > 1 且 aggregate delta > 0；
8. required runs 全部终态；
9. receipt 绑定 candidate/source/casepack/policy。

Formal 与 PMU 可配置为额外必需；PMU 即使必需仍只证明机制。迁移期旧 `promotion_gate.py`/`GATE.json` 同时生效，形成双重 fail-closed。

## 7. 4-op carrier 与榜面归因

`CarrierBundle` 保存 carrier 以及每个 component 的 source digest。一次 official submission 后，仅在以下条件满足时按 component 归因：

- after bundle 显式绑定 submission ID；
- before/after snapshot 的 task/hardware 一致；
- component 源码确实变化；
- before/after 都有该 component 分数行。

若多个 component 变化但只有 carrier total，结果标为 ambiguous，绝不把总增量平均或复制给每个算子。

## 8. 历史迁移

```bash
python cann_910c_pipeline/kcatctl.py --pretty --state-dir .kernelcat/910c \
  migrate-legacy --trajectory cann_910c_pipeline/r950_tools/TRAJECTORY.jsonl
```

默认 dry-run；加 `--apply` 才写 Ledger。原始 JSONL 先整体入 CAS，每行只保存安全结构化字段、原始行 SHA 与行号。只有同时具备 submission、job、before snapshot、after snapshot 才可标 official；其余均为 `legacy_derived`。`LAND` 本身永远不是 score proof。

## 9. Shadow rollout

### Phase A — shadow

`shadow-ab`、`shadow-profiler`、`shadow-submit` 只输出 secret-free request plan。旧执行器保持权威，新层比较终态事实。

### Phase B — dual-write

旧工具完成后，将 request/result/artifacts 写入 CAS/Ledger；旧文件继续保留。故障注入验证重试、lease、pending 恢复与幂等。

### Phase C — gate-authoritative

只有通过的新 GateReceipt 才能调用 official adapter；旧 gate 同时生效。

### Phase D — native executor

逐 backend 替换 adapter；每替换一个都要求 shadow parity 与回滚开关。KTP、32 卡开发机、profiler、official 不做一次性切换。

## 10. 低 token Agent 接口

CLI 默认输出单行 JSON，核心字段固定为 `ok`、`decision/status`、`reasons/checks`、内容 ID、artifact ref 与 provenance。Profiler compact view 只保留 Event decision、top PMU signals、gate 和 raw catalog ref；完整证据仍在 CAS，压 token 不等于删证据。

## 11. 常用命令

```bash
# 初始化与体检
python cann_910c_pipeline/kcatctl.py --pretty --state-dir .kernelcat/910c init
python cann_910c_pipeline/kcatctl.py --pretty --state-dir .kernelcat/910c doctor --repo-root .

# 冻结源码
python cann_910c_pipeline/kcatctl.py --pretty --state-dir .kernelcat/910c hash-tree 910c_tasks/<op>/arm

# 创建单机制 Policy
python cann_910c_pipeline/kcatctl.py --pretty --state-dir .kernelcat/910c create-policy \
  --operator <op> \
  --mechanism async_pipeline.shared_row_prefetch \
  --hypothesis "共享行只搬一次并复用" \
  --applicability "有限值域、小工作集" \
  --fallback "不满足谓词时调用 verified parent"

# Event 裁决 / 晋级门
python cann_910c_pipeline/kcatctl.py --pretty event-ab --input event_samples.json
python cann_910c_pipeline/kcatctl.py --pretty gate \
  --candidate candidate.json --evidence evidence.json \
  --current-parent parent_<sha> --record
```

## 12. 验证边界

CI 验证 Python 编译、领域契约、CAS/Ledger、Event/PMU 语义、Gate、carrier 归因、legacy 降级与备份 tree SHA。CI 没有 910C NPU，因此不会宣称 AscendC compile、correctness、full20、PMU 或 official score 已验证；这些必须由真实 adapter 运行后回填 EvidenceRecord。
