# Clean operator trial results

All runs below started with new sessions and no candidate. The configured 20-call ceiling is shared by parent, child, compaction and review; tokens are reported usage, not billing. No current run has full correctness, accepted paired speedup or promotion. No numerical speedup can be reported from host compilation failures.

Evidence root: `.validation/harness-trials/`. Each run retains its original hash-chained Journal; original device receipts, when present, remain under its trial directory. The verified index is `.validation/harness-trial-index.json`. Mechanisms and failure interpretation are detailed in `harness-optimization-log.md`.

| Trial / run | Calls P/C/compact/review | Known tokens | Usage missing | Seconds | Stages attempted | Remote job handles | E3 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `baseline-max-01/run-02` | 20/0/0/0 | 256,776 | 1 | 173.34 | A B C | 0 | no |
| `baseline-min-01/run` | 20/0/0/0 | 301,685 | 0 | 244.27 | A B C D E | 0 | no |
| `baseline-matmul-01/run` | 18/2/0/0 | 324,669 | 0 | 258.93 | A B C | 0 | no |
| `grounding-max-01/run` | 16/4/0/0 | 269,245 | 0 | 113.03 | A | 0 | no |
| `grounding-min-01/run` | 20/0/0/0 | 325,177 | 1 | 183.96 | A B C | 0 | no |
| `grounding-matmul-01/run` | 20/0/0/0 | 382,285 | 0 | 193.53 | A B C D E | 0 | no |
| `native-interface-max-01/run` | 20/0/0/0 | 380,805 | 0 | 133.20 | A B C | 0 | no |
| `native-interface-min-01/run` | 20/0/0/0 | 394,606 | 0 | 153.27 | A B C J | 0 | no |
| `native-interface-matmul-01/run` | 20/0/0/0 | 386,932 | 0 | 163.36 | A B C | 0 | no |
| `context-retention-max-01/run` | 7/0/0/0 | 106,940 | 0 | 42.38 | A | 0 | no |
| `context-retention-min-01/run` | 20/0/0/0 | 337,603 | 4 | 255.51 | A B C | 0 | no |
| `context-retention-matmul-01/run` | 16/4/0/0 | 336,169 | 1 | 163.14 | A B C | 0 | no |
| `stream16m-max-01/run` | 16/0/0/0 | 327,873 | 0 | 123.15 | A B C D E | 1 | no |
| `handle-wire-max-01/run` | 20/0/0/0 | 571,475 | 0 | 174.39 | A B C D E F | 2 | no |
| `handle-wire-min-01/run` | 20/0/0/0 | 506,216 | 0 | 216.82 | A B C J | 0 | no |
| `handle-wire-matmul-01/run` | 20/0/0/0 | 505,512 | 0 | 173.90 | A B C D E F | 1 | no |
| `hongkong-device1-feedback-max-01/run` | 20/0/0/0 | 657,244 | 0 | 554.91 | A B C D E | 0 | no |

Stage entries record calls, not successful acceptance. In particular, early J requests were rejected before a frozen candidate; F calls can return failed checks. The four original remote jobs across stream16m max and handle-wire max/Cube all failed in host compilation. Automatic IR/profiler consideration returned unavailable; no diagnostic capture or NPU timing occurred in these formal trials.

The zero-call startup preflight at `baseline-max-01/run` is retained but excluded. Prepared, unstarted stream16m min/Cube inputs are also excluded. Grounding Cube had a transport failure before a remote handle; original-namespace recovery evidence is retained, and no replacement was sent under that identity.

The device1 feedback trial used a fresh environment and independent device binding;
its timing is not a device0/device1 performance comparison. It generated one
candidate and called `pipeline_evaluate` at model calls 8, 18 and 20. The first
launch was rejected locally because the declared contract contained two extra
fields. That known pre-dispatch rejection was misclassified as an unknown effect,
and the resulting recovery fence prevented device work. The concrete error first
entered model input at call 12 after explicit log reads. All 20 protected call
progress snapshots matched the Journal. No compilation, NPU timing, IR/profiler
capture or E1/E3/H result occurred. Original details are indexed in
`.validation/hongkong-resume-20261111/trial-analysis/max/report.md`.
