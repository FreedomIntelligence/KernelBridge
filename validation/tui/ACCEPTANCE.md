# Local TUI acceptance — 2026-11-10

Status: local deterministic acceptance passed on macOS, Node 22.19.0 and Python 3.12. The implementation was completed before test authoring. Device qualification remains pending at the user's request.

Original artifacts: `.validation/tui-20261110-final/` in the checkout. The portable [result index](results/local-20261110.json) records check exits, durations and original-artifact SHA256 values. Reproduce with the [self-contained runner](README.md).

## Native Pi startup and official DeepSeek follow-up

The default entry now starts Pi 1.5.0's actual `InteractiveMode`, including its native header, theme, editor, completion, scrolling and keyboard handling. The earlier custom startup screen has been removed. Missing-model setup also uses `InteractiveMode`. A configured session starts a passive host immediately, with zero model or tool execution until explicit input; no Goal, Trial, seed file or workspace-history initialization is created at startup. An omitted budget remains unlimited and absent from the footer.

The [native Pi and DeepSeek result index](results/native-pi-deepseek-20261110.json) binds the current source and original artifacts. DeepSeek official configuration uses the `ds` / `deepseek-flash` and `ds-pro` / `deepseek-v4-pro` routes. Credentials are loaded from the private user file (mode 0600) or an overriding environment variable, including after host restart. No secret values enter project configuration or the evidence index.

| Check | Observed result |
| --- | --- |
| JavaScript and native regressions | 39 passed; TypeScript and managed-continuation checks passed |
| Native Pi codegen in a real PTY | 10 flows passed, 16 local fixture model calls; native idle startup, `/new` and shell before first prompt, direct read/edit/bash, consecutive input, compaction, interruption, explicit Goal, provider failure and resume |
| Missing model and explicit budget | Native setup/help/clear/exit and one-credit exhaustion passed |
| Official DeepSeek in a real PTY | 5 live model calls passed: read/edit/bash, completion returns idle, route selection, host restart from a fresh environment, no prompt replay and a resumed live response |
| Credential separation | Private file permissions verified; exact secret absent from checked source files and validation artifacts |

Final artifacts are `.validation/native-pi-unit-final.log`, `.validation/native-pi-final/`, `.validation/native-pi-setup-final/` and `.validation/deepseek-native-final-r3/`. The live check starts with `DEEPSEEK_API_KEY` removed from its environment and reads the user's private file. Pro route selection was checked; Pro inference was not run. The live configuration uses non-thinking mode.

Failed attempts remain available: the first older live-session restart correctly rejected changed runtime source; earlier PTY assertions expected a literal Pi logo or matched a prior `/new` receipt; two live harness attempts used an overlong socket path or incorrectly required a stale socket to disappear after SIGTERM. Final checks use the corrected assertions and the same current runtime across restart. No NPU, Linux/cross-host qualification or publication is claimed. The historical sections below retain their original run boundaries; their earlier no-host and no-real-model statements do not describe this follow-up.

## Earlier ordinary codegen follow-up

At this earlier checkpoint, the root `./kernelagent` opened a custom idle codegen editor in the selected project. No model request, host session, task scaffold, Goal or Trial was created until the user submitted input. The native Pi follow-up above supersedes that startup implementation. File and shell tools operate on that project. Omitted credit, step and Goal limits remain unset; the footer shows no budget. Goal scheduling and isolated device/evaluator workflows require explicit activation.

The [codegen result index](results/codegen-20261110.json) binds the final source, frozen Python modules and original artifacts. The canonical source-development launcher was exercised directly, including restart through `--continue`. This follows the user's revised ordinary-codegen contract; it does not claim feature-for-feature parity with other coding products.

| Check | Observed result |
| --- | --- |
| JavaScript, native tool, command and environment checks | 38 passed; TypeScript and managed-continuation validation also passed |
| Journal, lease, stream and codegen budget/control checks | 17 passed, including unbounded accounting beyond 200 credits and completed Goal controls |
| Existing real Pi/Executor regressions | 7 passed |
| Ordinary codegen in a real PTY | 35 additional user turns, 48 total local model requests; exact ordinary-call count, direct read/edit/bash, large project content, Chinese input, compaction, new conversation, interrupt, opt-in Goal, provider failure and resume passed |
| Missing model and explicit budget | Setup/help/clear/exit remained usable; one-credit initial-prompt limit stopped without editing, overspending or pending reservations |
| Actual default manual demo | Idle first, read/edit/bash, `!node --version`, consecutive user shell commands, clean tool output and exit passed |
| Final SDK regressions | Read/edit/evaluate, steering, worker/host recovery, provider failure and admission pause passed |
| Goal lifecycle | Pause, detach-continue, awaiting-user and three explicit-limit cases passed |
| Experiment-mode rendered TUI | Completion, Goal controls, visible command rejection, history navigation, latest submission and narrow footer passed |

Original final long-run artifacts are in `.validation/codegen-final-35/`. The short completion/clear/resume check is in `.validation/codegen-goal-clear-complete/`; the final SDK and lifecycle artifacts are in `.validation/codegen-sdk-complete/` and `.validation/codegen-lifecycle-complete/`. The manual demo is `.validation/codegen-demo-verified-r2/`. Reproduce using the [validation commands](README.md).

Failures were retained and corrected: short-context compaction left the editor populated; a queued/native input race duplicated delivery; a late interrupt could reopen a completed Goal. Test harness corrections distinguish retained historical Goal Trials from newly created Trials, and wait for the actual editor rather than the demo's pre-start banner. The final 35-turn run passed the corrected assertions. These are combined recorded runs; earlier failures are not presented as passing evidence. The isolated 20-turn SDK result remains from the earlier regression batch; final checks cover the changed input/control paths.

No real model endpoint or NPU was used. Vector/Cube device acceptance remains pending by the user's instruction; Linux/cross-host qualification and publication are not claimed. Existing terminals launched from an old installed bundle must be exited and reopened from the root launcher to use this implementation.

## Interaction follow-up

The [UI follow-up index](results/ui-20261110.json) records acceptance across the retained runs for the final runtime. It includes 43 unit/regression checks (13 authority, 23 Pi/tool/environment, 7 existing Executor), TypeScript, the seven SDK/host scenarios, and the original PTY scenario. A clean installation at `.validation/tui-install-ui/bundle` also passed the rendered-screen PTY checks: `/goal TEXT`, pause/resume/clear, actual Tab completion, visible rejection of unsupported commands, history-to-latest submission, Chinese input and a 48-column footer. Goal policy JSON and synthetic host-stop prose no longer appear as model output.

The deterministic admission test pauses after `prepare_turn`, before atomic model admission: no model runs or budget is spent at that boundary, no unresolved call remains, and the same worker resumes to three model calls and a full E1 fixture receipt. Python payload digests match across the two regression deployments and the installed runtime.

These results are combined from recorded runs, not a claim that the last parallel batch was entirely green. `.validation/tui-ui-regression-r2` passed all checks except an admission-test assertion that incorrectly treated a mapping as an array; the corrected assertion passed in `.validation/tui-ui-final`. That later parallel repeat hit resource-related timeouts in long-session, lifecycle and PTY checks; their earlier passes on the same runtime and all timeout logs are retained. The installed screen test passed in isolation with a 90-second per-condition deadline. The runner now defaults to one job, and PTY cleanup reaps its own stalled process group.

No real model endpoint or NPU was used. The prior acceptance tables below remain historical records of their stated runs.

| Check | Observed result |
| --- | --- |
| Journal/input/lease/evidence/stream unit checks | 13 passed |
| Pi patch, native tools and environment checks | 18 passed |
| Existing real Pi/Executor regressions | 7 passed |
| TypeScript | Passed |
| SDK read/edit/evaluate | 4 model requests, one E1 fixture receipt, 15 credits |
| Real PTY | Chinese multiline input, resize, long output, `!`/`!!`, reload/model, metered compaction, reset and terminal restoration passed |
| Long session | 20 additional user turns across worker lifetimes; compaction and fork; 28 total model calls, 39 credits, 2 Trials |
| Worker/host failure and truncated Pi JSONL | Original evaluator ran once; resumed with 4 model calls and 15 credits |
| Steering/follow-up/interrupt | Inputs persisted once, busy prompt rejected, interrupted input resumed through an explicit new turn |
| Background lifecycle | Pause, detach-continue and awaiting-user behavior passed; automatic Trial rollover stopped at 5 model calls and 3 Trials |
| Provider failure | One failed call, no automatic retry; explicit new input continued with a new action ID; 5 total model calls, 16 credits |
| Supplemental CPU, retained candidates, RSI snapshots, portable replay, installer and native-failure regressions | 15 passed |
| Clean-directory installation | Locked dependencies and all four Python packages installed successfully; the installed CLI accepted its runtime bindings |
| Installed CLI in fresh terminals | Exit/attach retained the same Pi session and Trial; budget continued from 24 to 25 credits; both terminal shutdowns passed |
| Installed host lifecycle and exhausted credits | All six cases passed; 1-credit initial-file denial and 7-credit model exhaustion both ended at `budget_limit`, with no remaining reservation |

The baseline evaluator is a small local Python fixture. E1 here means that fixture's full correctness contract; it is not an NPU result. Recovery fault injection includes an accepted local evaluation with its worker reply lost. It does not substitute for the remote NPU submission-key test.

Implementation review also corrected undeclared interactive plugin activation, bash writeback after failure, detach policy propagation between control and effect connections, stale-socket error propagation, worker-exit/lease-release races, provider-failure input status, cross-lease starts, incumbent selection across branches, and budget-denied initialization being mislabeled as awaiting user input. Unknown effects keep their reservations; only provably unstarted admissions can be cancelled and refunded.

The [installation and supplemental index](results/installation-20261110.json) binds these additional checks to their original logs and final runtime source hashes. The installed-entrypoint test includes the final Pi patch: exit hints use `kernelagent tui --attach`, and native project-trust guidance cannot offer an inapplicable resource-loading path. It waits for the host footer before submitting after attach, because Pi enables raw terminal mode before its submit handler is ready.

Real Linux execution, different-uid connection attempts, cross-host restore, Vector/Cube NPU evaluation, device 20-turn runs, capability promotion and publication remain unqualified. Prior delivery/device results are historical and are not reused as evidence for this implementation.
