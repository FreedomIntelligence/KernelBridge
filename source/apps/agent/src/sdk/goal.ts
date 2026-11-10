import { createHash } from "node:crypto";
import { open, readFile, rename, unlink } from "node:fs/promises";
import { join, dirname, isAbsolute } from "node:path";
import type { PrepareTrialRequest, ReplaceSessionRequest, RuntimeState } from "../generated/runtime.ts";
import { LoopXClient, LoopXFailure, object, textField, type JsonObject, type LoopXConnection } from "./loopx/client.ts";
import { claimGoalOwner, claimApplicationGoal, assertApplicationGoal, clearApplicationGoal } from "./loopx/ownership.mjs";
import { hostBridge } from "./loopx/bridge.ts";

export { LoopXFailure } from "./loopx/client.ts";
export interface GoalManagedRun {
  readonly resume?: string | ReplaceSessionRequest;
  readonly nextTrial?: string | PrepareTrialRequest;
  readonly signal?: AbortSignal;
}
export interface GoalManagedSession {
  readonly applicationSessionId: string;
  readonly status: string;
  readonly hasPendingInput: boolean;
  run(request: GoalManagedRun): Promise<Record<string, unknown>>;
  interrupt(reason?: string): unknown;
  state(): Promise<RuntimeState>;
}
export interface LoopXGoalOptions extends LoopXConnection {
  readonly goalId: string;
  readonly agentId: string;
  readonly todoId: string;
  readonly applicationSessionId: string;
  readonly validatorArgv: readonly string[];
  readonly validatorTimeoutSeconds: number;
}
export interface GoalObservation {
  readonly goalId: string;
  readonly activation: "active" | "paused";
  readonly stateFingerprint: string;
  readonly actions: JsonObject;
  readonly quota: JsonObject;
  readonly acceptance: JsonObject;
}
export interface GoalTurnRequest {
  readonly intentId: string;
  readonly run: GoalManagedRun;
  readonly completionCandidate?: boolean;
  readonly signal?: AbortSignal;
}
export interface GoalNextStop {
  readonly stop: { readonly reason: string };
}
export interface GoalContinuationResult {
  readonly status: "progress" | "completed" | "blocked" | "paused" | "cleared" | "budget_limit" |
    "queued_input" | "interrupted" | "no_progress" | "validation_failed" | "recovery_required" | "replayed";
  readonly turnKey?: string;
  readonly observation?: JsonObject;
}
const identityPattern = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/;
const keyPattern = /^sha256:[a-f0-9]{64}$/;
function canonical(value: unknown): string {
  if (value === undefined) return "null";
  if (value === null || typeof value !== "object") return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
  return `{${Object.entries(value).filter(([, item]) => item !== undefined).sort(([a], [b]) => a < b ? -1 : a > b ? 1 : 0).map(([key, item]) => `${JSON.stringify(key)}:${canonical(item)}`).join(",")}}`;
}
const digest = (value: unknown): string => createHash("sha256").update(canonical(value)).digest("hex");

async function save(path: string, value: JsonObject): Promise<void> {
  const temporary = `${path}.${process.pid}.tmp`;
  const file = await open(temporary, "wx", 0o600);
  try { await file.writeFile(JSON.stringify(value)); await file.sync(); } finally { await file.close(); }
  await rename(temporary, path);
  const directory = await open(dirname(path), "r");
  try { await directory.sync(); } finally { await directory.close(); }
}

/** LoopX owns lifecycle and receipts. This handle stores only cross-owner identities. */
export class LoopXGoal {
  private fenced = false;
  private detached = false;
  private advancing = false;
  private activeAbort: AbortController | undefined;
  private readonly client: LoopXClient;
  readonly options: LoopXGoalOptions;
  private readonly session: GoalManagedSession;
  private readonly directory: string;
  private constructor(client: LoopXClient, options: LoopXGoalOptions, session: GoalManagedSession, directory: string) {
    this.client = client;
    this.options = options;
    this.session = session;
    this.directory = directory;
  }

  static async bind(options: LoopXGoalOptions, session: GoalManagedSession): Promise<LoopXGoal> {
    for (const key of ["goalId", "agentId"] as const) {
      if (!identityPattern.test(options[key])) throw new Error(`Invalid ${key}`);
    }
    if (!options.applicationSessionId || options.applicationSessionId.length > 512 || options.applicationSessionId !== session.applicationSessionId) throw new Error("Application session identity mismatch");
    if (!/^todo_[A-Za-z0-9_-]{1,128}$/.test(options.todoId)) throw new Error("Invalid todoId");
    if (!options.validatorArgv.length || !options.validatorArgv[0]?.startsWith("/")) throw new Error("Validator requires explicit absolute executable argv");
    if (!Number.isFinite(options.validatorTimeoutSeconds) || options.validatorTimeoutSeconds <= 0 || options.validatorTimeoutSeconds > 120) throw new Error("Invalid validation timeout");
    const client = await LoopXClient.connect(options);
    const frozen = Object.freeze({ ...options, ...client.options, validatorArgv: Object.freeze([...options.validatorArgv]) });
    const directory = await claimGoalOwner(frozen.runtimeRoot, frozen.goalId, {
      owner: "sdk", goalId: frozen.goalId, agentId: frozen.agentId, todoId: frozen.todoId,
      applicationSessionId: frozen.applicationSessionId, bindingDigest: digest(frozen),
    });
    await claimApplicationGoal(frozen.runtimeRoot, frozen.applicationSessionId, frozen.goalId);
    const goal = new LoopXGoal(client, frozen, session, directory);
    await goal.read();
    return goal;
  }

  static async previewCreate(options: LoopXConnection, objective: string, goalId: string): Promise<JsonObject> {
    if (!objective.trim() || !identityPattern.test(goalId)) throw new Error("Explicit goal identity and objective required");
    const client = await LoopXClient.connect(options);
    return await client.call(["start-goal", "--guided", "--project", client.options.project,
      "--goal-id", goalId, "--goal-text", objective]);
  }

  static async create(options: LoopXConnection, request: { readonly goalId: string; readonly objective: string; readonly stateFile: string }): Promise<JsonObject> {
    if (!identityPattern.test(request.goalId) || !request.objective.trim() || request.objective.length > 16000 || !isAbsolute(request.stateFile)) throw new Error("Invalid Goal creation request");
    const client = await LoopXClient.connect(options, true);
    const args = ["bootstrap", "--project", client.options.project, "--goal-id", request.goalId,
      "--objective", request.objective, "--state-file", request.stateFile,
      "--adapter-kind", "generic_project_goal_v0", "--adapter-status", "connected", "--no-global-sync"];
    const preview = await client.call([...args, "--dry-run"]);
    if (preview.registry_goal_action !== "appended" || preview.state_action !== "created") throw new Error("Goal creation refuses existing Goal or state");
    const result = await client.call(args);
    if (result.goal_id !== request.goalId || result.registry !== client.options.registry || result.state_file !== request.stateFile ||
        result.registry_goal_action !== "appended" || result.state_action !== "created") throw new LoopXFailure("Goal creation identity conflict; inspect owner state", "unknown", result);
    const state = await readFile(request.stateFile, "utf8");
    const objective = state.split("\n").find(line => line.startsWith("objective: "))?.slice(11);
    if (objective === undefined || JSON.parse(objective) !== request.objective) throw new LoopXFailure("Goal objective readback mismatch", "unknown");
    return { ...result, objective: request.objective, binding_status: "requires_agent_and_todo" };
  }

  async read(): Promise<GoalObservation> {
    const o = this.options;
    const actions = await this.client.call(["goal-actions", "--goal-id", o.goalId]);
    if (actions.goal_id !== o.goalId || !["active", "stopped"].includes(String(actions.activation_state))) throw new Error("LoopX Goal binding changed");
    const fingerprint = textField(actions, "state_fingerprint");
    if (!/^[a-f0-9]{64}$/.test(fingerprint)) throw new Error("Invalid source fingerprint");
    const quota = await this.client.call(["quota", "should-run", "--goal-id", o.goalId, "--agent-id", o.agentId, "--runtime-profile", "generic_cli"], undefined, true);
    if (quota.goal_id !== o.goalId) throw new Error("LoopX quota binding changed");
    const acceptance = await this.client.call(["goal-acceptance", "inspect", "--goal-id", o.goalId, "--agent-id", o.agentId], undefined, true);
    return { goalId: o.goalId, activation: actions.activation_state === "active" ? "active" : "paused",
      stateFingerprint: fingerprint, actions, quota, acceptance };
  }

  async pause(expectedFingerprint: string, reason = "Paused by application user"): Promise<JsonObject> {
    this.fence();
    return await this.transition("stop", expectedFingerprint, reason);
  }
  async resume(expectedFingerprint: string, reason = "Resumed by application user"): Promise<JsonObject> {
    if (this.detached) throw new Error("Cleared Goal must be explicitly rebound");
    await assertApplicationGoal(this.options.runtimeRoot, this.options.applicationSessionId, this.options.goalId);
    const result = await this.transition("resume", expectedFingerprint, reason);
    this.fenced = false;
    return result;
  }
  async clear(expectedFingerprint: string): Promise<JsonObject> {
    const result = await this.pause(expectedFingerprint, "Cleared from application session; evidence retained");
    await clearApplicationGoal(this.options.runtimeRoot, this.options.applicationSessionId, this.options.goalId);
    this.detached = true;
    return result;
  }
  private fence(): void {
    this.fenced = true;
    this.activeAbort?.abort();
    if (this.session.status === "running") this.session.interrupt("Goal stopped by user");
  }
  private async transition(operation: "stop" | "resume", fingerprint: string, reason: string): Promise<JsonObject> {
    if (!/^[a-f0-9]{64}$/.test(fingerprint) || !reason.trim() || reason.length > 512) throw new Error("Invalid lifecycle condition");
    const result = await this.client.call(["goal-lifecycle", "--goal-id", this.options.goalId,
      "--operation", operation, "--actor-kind", "owner", "--expected-state-fingerprint", fingerprint,
      "--reason", reason, "--execute"]);
    if (result.goal_id !== this.options.goalId || object(result.readback, "activation readback").verified !== true) throw new LoopXFailure("Lifecycle readback unverified", "unknown", result);
    return result;
  }
  inspectTurn(turnKey: string): Promise<JsonObject> {
    if (!keyPattern.test(turnKey)) throw new Error("Exact Turn key required");
    return this.client.call(["turn", "inspect-journal", "--goal-id", this.options.goalId,
      "--agent-id", this.options.agentId, "--turn-key", turnKey]);
  }
  private stopReason(): GoalContinuationResult["status"] | undefined {
    if (this.detached) return "cleared";
    if (this.fenced) return "paused";
    if (this.session.hasPendingInput) return "queued_input";
    if (this.session.status !== "idle") return "interrupted";
    return undefined;
  }
  async advance(request: GoalTurnRequest): Promise<GoalContinuationResult> {
    if (!identityPattern.test(request.intentId)) throw new Error("Stable intentId required");
    if (this.advancing) throw new Error("A Goal continuation is already in flight");
    const stop = this.stopReason();
    if (stop) return { status: stop };
    this.advancing = true;
    const abort = new AbortController();
    this.activeAbort = abort;
    const timer = setTimeout(() => { abort.abort(); this.session.interrupt("Goal deadline elapsed"); }, this.options.timeoutMs);
    const cancel = (): void => { abort.abort(); this.session.interrupt("Goal Turn cancelled"); };
    request.signal?.addEventListener("abort", cancel, { once: true });
    if (request.signal?.aborted) cancel();
    try { return await this.exclusiveAdvance(request, abort.signal); }
    finally { clearTimeout(timer); request.signal?.removeEventListener("abort", cancel); this.activeAbort = undefined; this.advancing = false; }
  }
  /** Advance at most maxTurns; next supplies an explicit admitted next-Trial request. */
  async pursue(request: { readonly first: GoalTurnRequest; readonly maxTurns: number;
    readonly next: (previous: GoalContinuationResult, index: number) => Promise<GoalTurnRequest | GoalNextStop | undefined>;
  }): Promise<readonly GoalContinuationResult[]> {
    if (!Number.isSafeInteger(request.maxTurns) || request.maxTurns < 1 || request.maxTurns > 100) throw new Error("maxTurns must be 1..100");
    const results: GoalContinuationResult[] = [];
    let current: GoalTurnRequest | undefined = request.first;
    for (let index = 0; index < request.maxTurns && current; index += 1) {
      const result = await this.advance(current);
      results.push(result);
      if (result.status !== "progress" || this.stopReason()) break;
      if (index + 1 < request.maxTurns) {
        const next = await request.next(result, index + 1);
        const stop = this.stopReason();
        if (stop) {
          results.push({ status: stop });
          break;
        }
        if (next === undefined || "stop" in next) {
          const reason = next?.stop.reason ?? "Caller supplied no next Trial";
          results.push(await this.recordPlanningStop(reason));
          break;
        }
        current = next;
      }
    }
    return results;
  }
  private async recordPlanningStop(reason: string): Promise<GoalContinuationResult> {
    if (typeof reason !== "string" || !reason.trim() || reason.length > 512) {
      throw new Error("Next-Trial stop reason must contain 1..512 characters");
    }
    reason = reason.trim().replace(/\s+/g, " ");
    const observation = await this.read();
    const localStop = this.stopReason();
    if (localStop) return { status: localStop };
    if (observation.activation !== "active") return { status: "paused" };
    const acceptance = object(observation.acceptance.goal_acceptance_contract ?? {}, "acceptance contract");
    if (acceptance.status === "accepted") return { status: "completed", observation: observation.acceptance };
    const result = await this.pause(observation.stateFingerprint, reason);
    if (result.written !== true || object(result.activation, "planning stop activation").reason !== reason) {
      throw new LoopXFailure("Planning stop reason readback differs from request", "unknown", result);
    }
    return { status: "paused", observation: { reason, lifecycle: result } };
  }
  private async exclusiveAdvance(request: GoalTurnRequest, signal: AbortSignal): Promise<GoalContinuationResult> {
    await assertApplicationGoal(this.options.runtimeRoot, this.options.applicationSessionId, this.options.goalId);
    const path = join(this.directory, `${digest(this.options.goalId)}.advancing`);
    let file;
    try { file = await open(path, "wx", 0o600); }
    catch (error) {
      if ((error as NodeJS.ErrnoException).code === "EEXIST") return { status: "recovery_required" };
      throw error;
    }
    try { await file.writeFile(request.intentId); await file.sync(); } finally { await file.close(); }
    const result = await this.advanceOnce(request, signal);
    // A crash or uncertain outcome retains this fence; never steal an abandoned claim.
    if (!["recovery_required", "validation_failed"].includes(result.status)) await unlink(path);
    return result;
  }
  private async advanceOnce(request: GoalTurnRequest, signal: AbortSignal): Promise<GoalContinuationResult> {
    const observation = await this.read();
    if (observation.activation !== "active") return { status: "paused" };
    if (object(observation.acceptance.goal_acceptance_contract ?? {}, "acceptance contract").status === "accepted") return { status: "completed", observation: observation.acceptance };
    if (observation.quota.should_run !== true) return { status: observation.quota.pause_cause === "compute_quota_zero" ? "budget_limit" : "blocked", observation: observation.quota };
    const o = this.options;
    const path = join(this.directory, `${digest([o.goalId, o.applicationSessionId])}.turn.json`);
    const run = { ...request.run, signal: undefined };
    const identity = digest({ intentId: request.intentId, run, completion: request.completionCandidate === true });
    let previous: JsonObject | undefined;
    try { previous = object(JSON.parse(await readFile(path, "utf8")), "Goal cursor"); }
    catch (error) { if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error; }
    if (previous) {
      if (previous.intentId === request.intentId && previous.requestDigest !== identity) throw new Error("Goal intent reused with different input");
      if (typeof previous.turnKey !== "string") return { status: "recovery_required" };
      const prior = await this.inspectTurn(previous.turnKey);
      if (prior.journal_status !== "committed") return { status: "recovery_required", turnKey: previous.turnKey, observation: prior };
      // A known committed duplicate is read-only. It must release this fresh
      // claim rather than turn successful delivery into an unresolved operation.
      if (previous.intentId === request.intentId) return { status: "replayed", turnKey: previous.turnKey, observation: prior };
      if (request.run.nextTrial === undefined) throw new Error("Next Goal Turn requires an explicit next-Trial intent");
    }
    if (previous) {
      const state = await this.session.state();
      if (state.pending_effect_ids.length) return { status: "recovery_required" };
      if ([state.account_budget, state.run_budget].some(budget => budget.spent + budget.reserved >= budget.limit)) return { status: "budget_limit" };
    }
    const stop = this.stopReason();
    if (stop || signal.aborted) return { status: stop ?? "interrupted" };
    const cursor: JsonObject = { intentId: request.intentId, requestDigest: identity };
    await save(path, cursor);
    let managed: JsonObject | undefined;
    let hostStop: GoalContinuationResult["status"] | undefined;
    try {
      const result = await hostBridge(o.node, async host => {
        const envelope = object(host.turn_envelope, "Turn envelope");
        const action = object(envelope.action, "Turn action");
        const todo = object(action.selected_todo, "selected Todo");
        const key = textField(host, "turn_key");
        if (host.schema_version !== "loopx_turn_host_request_v0" || !keyPattern.test(key) || host.route !== "ready_for_host" ||
            envelope.goal_id !== o.goalId || envelope.agent_id !== o.agentId || envelope.should_run !== true || todo.todo_id !== o.todoId ||
            object(envelope.execution_policy, "execution policy").normal_delivery_allowed !== true) throw new Error("LoopX host identity mismatch");
        cursor.turnKey = key;
        await save(path, cursor);
        await assertApplicationGoal(o.runtimeRoot, o.applicationSessionId, o.goalId);
        if (this.stopReason() || signal.aborted) throw new Error("Goal continuation fenced");
        managed = await this.session.run({ ...request.run, signal });
        if (managed.settlement !== "settled" || !Array.isArray(managed.pending_effect_ids) || managed.pending_effect_ids.length) throw new Error("Managed Trial has unresolved effects");
        if (managed.execution_state !== "CANDIDATES_READY" || !managed.candidate_program_id) {
          const state = await this.session.state();
          hostStop = [state.account_budget, state.run_budget, state.trial_budget].some(budget => budget.spent + budget.reserved >= budget.limit) ? "budget_limit" : "no_progress";
          throw new Error("Managed Trial has no candidate progress");
        }
        const state = await this.session.state();
        const candidate = state.graph.objects.find(item => item.kind === "program" && item.program_id === managed?.candidate_program_id);
        if (candidate?.kind !== "program" || candidate.artifact_digest === state.parent_program.artifact_digest) {
          hostStop = "no_progress";
          throw new Error("Managed candidate has no independently observable artifact change");
        }
        if (this.fenced || signal.aborted || this.session.hasPendingInput) throw new Error("Goal was fenced during managed execution");
        return { schema_version: "loopx_turn_result_v0", turn_key: key,
          result_kind: "validated_progress",
          completed_phases: ["host_execute", "typed_result"], classification: "kernelcat_managed_candidate",
          summary: `Managed candidate receipt sha256:${digest(managed)}`,
          recommended_action: "Independently validate the original objective and retained candidate.",
          next_action: "Continue only after authoritative validation and fresh admission.",
          vision_unchanged_reason: "The frozen managed task and bound Goal are unchanged.",
          delivery_batch_scale: "single_surface", delivery_outcome: "outcome_progress", path_delta_mode: "unchanged" };
      }, async hostArgv => await this.client.call(["turn", "run-once", "--goal-id", o.goalId, "--agent-id", o.agentId,
        "--host", "generic-cli", "--execution-mode", "isolated-headless", "--scheduler-owner", "outer_controller",
        "--todo-id", o.todoId, "--turn-instance-id", `sdk-${digest([o.applicationSessionId, request.intentId])}`,
        "--iteration-context", "fresh", "--project", o.project, "--scan-root", o.project,
        "--host-command-json", JSON.stringify(hostArgv), "--validation-command-json", JSON.stringify(o.validatorArgv),
        "--validation-timeout-seconds", String(o.validatorTimeoutSeconds), "--timeout-seconds", String(o.timeoutMs / 1000),
        "--no-global-sync", "--execute"], signal));
      const turnKey = textField(cursor, "turnKey");
      if (result.resume_turn_key !== turnKey) throw new LoopXFailure("LoopX returned a different Turn identity", "unknown", result);
      if (result.status !== "committed" || object(result.receipt, "Turn receipt").ok !== true) return { status: "validation_failed", turnKey, observation: result };
      if (request.completionCandidate) {
        // The generic host commits the checked Turn first. Todo completion uses
        // LoopX's lifecycle owner, which runs the configured acceptance checks.
        const completion = await this.client.call(["todo", "complete", "--goal-id", o.goalId,
          "--todo-id", o.todoId, "--agent-id", o.agentId, "--no-follow-up",
          "--reason", "The configured task acceptance checks passed for the retained candidate.",
          "--note", `Validated managed candidate from ${turnKey}`,
          "--evidence", `turn:${turnKey}`, "--validation-command-json", JSON.stringify(o.validatorArgv),
          "--validation-timeout-seconds", String(Math.min(o.validatorTimeoutSeconds, 25)),
          "--turn-instance-id", `sdk-${digest([o.applicationSessionId, request.intentId])}`], signal);
        const accepted = await this.client.call(["goal-acceptance", "verify", "--goal-id", o.goalId, "--agent-id", o.agentId, "--execute"], signal);
        return { status: accepted.acceptance_ready === true && accepted.checks_passed === true ? "completed" : "validation_failed", turnKey,
          observation: { ...result, completion, goal_acceptance: accepted } };
      }
      return { status: result.result_kind === "validated_progress" ? "progress" : "no_progress", turnKey, observation: result };
    } catch (error) {
      if (!(error instanceof LoopXFailure)) return { status: "recovery_required", ...(typeof cursor.turnKey === "string" ? { turnKey: cursor.turnKey } : {}) };
      return { status: hostStop ?? (error.outcome === "unknown" ? "recovery_required" : "validation_failed"),
        ...(typeof cursor.turnKey === "string" ? { turnKey: cursor.turnKey } : {}), observation: error.observation };
    }
  }
}
