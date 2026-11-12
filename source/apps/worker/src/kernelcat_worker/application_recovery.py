"""Reconcile original receipts before restoring a native conversation boundary."""
import re
import json
from pathlib import Path

from kernelcat_executor.operations.recovery import _root_attempt
from kernelcat_executor.contracts.messages import AssistantTurn, turn_actions
from kernelcat_executor.contracts.models import Action, Conflict, InDoubt, Outcome, canonical, digest
from kernelcat_executor.operations.control import SessionControl


def reconcile_backgrounds(host):
    """Recover original child identities and replay lost completion notifications.

    This only runs while restoring a worker, never during a live child lane.
    Uncertain child effects retain their original fence; nothing is redispatched.
    """
    gateway = host.owner.gateway
    actions = gateway.store.snapshot()["actions"]
    for identity, item in actions.items():
        action, started = item["action"], item.get("outcome", {})
        if (action["task"] != host.task.id or action["tool"] != "delegate"
                or started.get("status") != "completed" or not started.get("data", {}).get("background")):
            continue
        result = actions.get(identity + "/background")
        if result is None:
            child_actions = [row for row in actions.values() if row["action"]["task"] == identity + "/leaf"]
            if any(not row.get("terminal") or row.get("outcome", {}).get("status") == "unknown" for row in child_actions):
                continue
            changes = {}
            descriptor = started.get("data", {}).get("worktree")
            if descriptor:
                from kernelcat_executor.capabilities.commands import Worktree
                try:
                    changes = {"worktree": Worktree.recover(descriptor).finish("kernelbridge recovered subagent")}
                except Exception as error:
                    changes = {"worktree": {**descriptor, "changed": None, "error": str(error)[:400]}}
            outcome = gateway.internal(Action(id=identity + "/background", task=action["task"],
                tool="__background_result__", arguments={"agent_id": identity}, parents=(identity,)), 0,
                lambda: Outcome(status="failed", data={"reason": "worker_exited", "recovered": True,
                    "agent": action["arguments"]["agent"], "advisory": True, "promoted": False, **changes}))
        elif not result.get("terminal") or result.get("outcome", {}).get("status") == "unknown":
            continue
        else:
            outcome = Outcome.model_validate_json(canonical(result["outcome"]))
        host.background_settled(Action.model_validate_json(canonical(action)), outcome)


def recovery_plan(host, *, cancel_unstarted=False):
    store = host.owner.gateway.store
    state = store.snapshot()
    actions = state["actions"]
    if cancel_unstarted:
        for item in actions.values():
            task_id = item["action"]["task"]
            if (task_id == host.task.id or task_id.startswith(host.task.id + "/")) and not any(
                    item.get(key) for key in ("terminal", "started", "outcome")):
                store.cancel_unstarted(Action.model_validate_json(canonical(item["action"])))
        reconcile_backgrounds(host)
        actions = store.snapshot()["actions"]
    pending = [key for key, item in actions.items() if not item.get("terminal")
               or item.get("outcome", {}).get("status") == "unknown"]
    if pending and cancel_unstarted and host.owner.rsi is not None:
        for key in pending:
            item = actions[key]
            if item["action"]["tool"] == "__rsi_adapter__" and item.get("started"):
                host.owner.rsi.adapter.collect_pending(host.owner.gateway, host.task.id, key)
        actions = store.snapshot()["actions"]
        pending = [key for key, item in actions.items() if not item.get("terminal")
               or item.get("outcome", {}).get("status") == "unknown"]
    if pending:
        raise InDoubt("original effects require reconciliation: " + ", ".join(pending))
    ended = {item["outcome"]["data"]["proposal_id"] for item in actions.values()
             if item["action"]["tool"] == "__turn_end__" and item.get("terminal")}
    turns = {}
    snapshot = store.snapshot()
    for identity, item in actions.items():
        action, outcome = item["action"], item.get("outcome", {})
        if action["task"] != host.task.id or action["tool"] != "__propose__" or identity in ended or not item.get("terminal"):
            continue
        # An automatic retry is the deterministic successor of its step's attempt;
        # follow the hash links back to that step rather than guessing from the ID.
        base = identity
        if identity.startswith("__model_retry__/"):
            try:
                base = _root_attempt(snapshot, identity, host.task.id)
            except Conflict:
                continue
        match = re.search(r"/step/(\d+)/model/", base)
        if not match:
            continue  # Compaction is metered but is not a conversational turn.
        step = int(match[1])
        # A step with a turn-end record is closed, whichever attempt it names
        # (a retry chain leaves earlier failed attempts behind).
        if f"{host.task.id}/step/{step}/end" in actions:
            continue
        if outcome.get("status") != "completed":
            if step not in turns or turns[step]["model_receipt"].get("status") != "completed":
                turns[step] = {"step": step, "proposal_id": identity, "model_receipt": outcome,
                               "calls": [], "parallel": False}
            continue
        turn = AssistantTurn.model_validate_json(canonical(outcome["data"]))
        parallel = identity + "/batch" in actions
        calls = turn_actions(turn, host.task.id, identity, parallel=parallel)
        receipts = []
        for call in calls:
            recorded = actions.get(call.id)
            if recorded is None and cancel_unstarted:
                cancelled = host.owner.gateway.internal(call, 0, lambda: Outcome(status="failed", data={
                    "unexecuted": True, "recovered": True, "reason": "Worker was lost before this call started"}))
                recorded = {"outcome": cancelled.model_dump(mode="json"), "terminal": True}
            if recorded is not None and (not recorded.get("terminal") or recorded["outcome"]["status"] == "unknown"):
                raise InDoubt(call.id)
            receipts.append({"tool_call_id": "kc_" + digest(call.id), "action_id": call.id,
                "tool_name": call.tool, "arguments": call.arguments,
                "outcome": recorded["outcome"] if recorded else {"status": "failed", "data": {"unexecuted": True}}})
        turns[step] = {"step": step, "proposal_id": identity, "model_receipt": outcome,
                       "calls": receipts, "parallel": parallel}
    application = host.journal.state()
    return {"turns": [turns[step] for step in sorted(turns)], "checkpoint": application.get("checkpoint"),
            "workspace": host.owner.workspace_policy["root"], "directory": str(host.owner.replay.directory),
            "pending_transition": application.get("pending_transition"),
            "applied_inputs": [item["input_id"] for item in application["inputs"].values() if item["status"] == "applied"],
            "pending_calls": [key for key, item in application["calls"].items() if item["status"] == "pending"]}


def commit_recovery(host, connection, request):
    plan = recovery_plan(host, cancel_unstarted=True)
    if digest(plan) != request["plan_digest"]:
        raise Conflict("recovery evidence changed before checkpoint commit")
    checkpoint = host.owner.replay.checkpoint(request["checkpoint"]) if request.get("checkpoint") else None
    if plan["turns"] and checkpoint is None:
        raise Conflict("recovery of model turns requires a native checkpoint")
    control = SessionControl(host.owner.gateway, host.owner.options)
    if plan["pending_transition"]:
        from kernelcat_worker.application_artifacts import restore_source, read_source_snapshot
        parent = plan["pending_transition"]["parent"]
        path = host.owner.gateway.store.path.parent / "checkpoints" / (parent["source_sha256"] + ".json")
        policy = host.owner.workspace_policy
        restore_source(Path(policy["root"]), read_source_snapshot(path, parent["source_sha256"]), policy["writable_paths"], policy["readonly_paths"])
        host.journal.command("configuration", {**connection.worker, "operation": "transition_aborted",
            "transition_id": plan["pending_transition"]["transition_id"], "reason": "worker_lost_before_trial_commit"})
        host.journal.command("checkpoint", {**parent, **connection.worker})
    for turn in plan["turns"]:
        parents = tuple(item["action_id"] for item in turn["calls"])
        prior = parents[-1] if parents else turn["proposal_id"]
        control.checkpoint(host.task.id, turn["step"], turn["proposal_id"], prior, None,
                           parents=parents if turn["parallel"] else None, native_checkpoint=checkpoint)
    for call_id in plan["pending_calls"]:
        host.journal.command("call_reconciled", {"call_id": call_id, "checkpoint": checkpoint,
            "evidence_refs": [turn["proposal_id"] for turn in plan["turns"]]})
    if checkpoint and plan.get("checkpoint"):
        host.journal.command("checkpoint", {**plan["checkpoint"], **connection.worker,
            "native_checkpoint": checkpoint, "pi_session_id": checkpoint["session_id"], "entry_id": checkpoint["leaf_id"],
            "session_file": str(host.owner.replay.directory / "checkpoints" / (checkpoint["digest"][7:] + ".jsonl"))})
    for value in host.journal.state()["inputs"].values():
        if value["status"] == "applied":
            host.journal.command("transition", {**connection.worker, "input_id": value["input_id"], "status": "interrupted"})
    return {"status": "reconciled", "checkpoint": checkpoint}
