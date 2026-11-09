import { isAbsolute } from "node:path";
import { runtimeValue } from "../clients/runtime.ts";
import { DEFAULT_WORKER_FRAME_BYTES, DEFAULT_WORKER_PAYLOAD_BYTES } from "../clients/worker-client.ts";
import type { GoalPursuitBinding } from "../generated/runtime.ts";
import { MAX_CONTROL_BYTES, readBoundedUtf8 } from "../tasks/launch-manifest.ts";
import { KernelCat } from "./index.ts";
import { LoopXGoal } from "./goal.ts";
import type { GoalContinuationResult } from "./goal.ts";
import { createStageRouterNext } from "./next-trial.ts";

export interface GoalPursuitOptions {
  readonly signal?: AbortSignal;
}
export interface GoalPursuitResult extends Record<string, unknown> {
  readonly status: GoalContinuationResult["status"] | "invocation_limit";
  readonly goal_id: string;
  readonly application_session_id: string;
  readonly turns: readonly GoalContinuationResult[];
}

/** Binding validation starts no process and never provisions Goal/Agent/Todo state. */
export async function loadGoalPursuitBinding(path: string): Promise<GoalPursuitBinding> {
  const text = await readBoundedUtf8(path, "Goal pursuit binding", MAX_CONTROL_BYTES);
  const binding = runtimeValue("GoalPursuitBinding", JSON.parse(text));
  const { worker, session, loopx, goal, pursuit } = binding;
  if (pursuit.resume !== undefined && pursuit.next_trial !== undefined) {
    throw new Error("Goal pursuit requires at most one explicit resume or next-Trial request");
  }
  const paths = {
    python: worker.python, config: worker.config, expected_python_modules: worker.expected_python_modules,
    manifest: session.manifest, cli: loopx.cli, node: loopx.node, project: loopx.project,
    registry: loopx.registry, runtime_root: loopx.runtime_root, home: loopx.home,
    validator_executable: goal.validator_argv[0],
  };
  for (const [field, value] of Object.entries(paths)) {
    if (!isAbsolute(value) || value.includes("\0")) {
      throw new Error(`Goal pursuit ${field} must be an explicit absolute path without NUL`);
    }
  }
  return binding;
}

/** The only automatic execution loop is LoopXGoal.pursue; this adapter owns cleanup. */
export async function pursueGoal(path: string, options: GoalPursuitOptions = {}): Promise<GoalPursuitResult> {
  options.signal?.throwIfAborted();
  const binding = await loadGoalPursuitBinding(path);
  const { worker, session: selected, loopx, goal: selectedGoal, pursuit } = binding;
  const client = new KernelCat({
    python: worker.python, config: worker.config, expectedPythonModules: worker.expected_python_modules,
    timeoutMs: worker.timeout_ms ?? 300000, maxFrameBytes: worker.max_frame_bytes ?? DEFAULT_WORKER_FRAME_BYTES,
    maxPayloadBytes: worker.max_payload_bytes ?? DEFAULT_WORKER_PAYLOAD_BYTES, signal: options.signal,
  });
  const session = await client.createSession({ manifest: selected.manifest, applicationSessionId: selected.application_session_id });
  try {
    const goal = await LoopXGoal.bind({
      cli: loopx.cli, node: loopx.node, project: loopx.project, registry: loopx.registry,
      runtimeRoot: loopx.runtime_root, home: loopx.home, timeoutMs: loopx.timeout_ms ?? 60000,
      maxOutputBytes: loopx.max_output_bytes, goalId: selectedGoal.goal_id,
      agentId: selectedGoal.agent_id, todoId: selectedGoal.todo_id,
      applicationSessionId: session.applicationSessionId, validatorArgv: selectedGoal.validator_argv,
      validatorTimeoutSeconds: selectedGoal.validator_timeout_seconds,
    }, session);
    const nextTrial = createStageRouterNext(session, {
      manifest: selected.manifest, intentId: pursuit.intent_id, trialCreditLimit: pursuit.trial_credit_limit,
    });
    const turns = await goal.pursue({
      first: { intentId: pursuit.intent_id, signal: options.signal,
        run: { resume: pursuit.resume, nextTrial: pursuit.next_trial, signal: options.signal } },
      maxTurns: pursuit.max_turns,
      next: async (previous, index) => {
        options.signal?.throwIfAborted();
        const next = await nextTrial(previous, index);
        return "stop" in next ? next : { ...next, signal: options.signal };
      },
    });
    const last = turns[turns.length - 1];
    return {
      status: last.status === "progress" ? "invocation_limit" : last.status,
      goal_id: selectedGoal.goal_id, application_session_id: session.applicationSessionId, turns,
    };
  } finally {
    await session.dispose();
  }
}
