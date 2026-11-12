"""Deterministic byte-bounded model projection; the journal remains lossless."""
from __future__ import annotations

import hashlib
from collections.abc import Callable

from kernelcat_executor.contracts.context import ContextOverflow as ContextOverflow
from kernelcat_executor.contracts.context import _size
from kernelcat_executor.contracts.context import validate_submission as validate_submission
from kernelcat_executor.contracts.models import (
    Json,
    Policy,
    Task,
    canonical,
    digest,
)
from kernelcat_executor.operations.control import input_batches
from kernelcat_executor.operations.turns import native_proposals, project_turns
from kernelcat_executor.contracts.text import fit_observation_page


def _preview(value: object, limit: int) -> object:
    return _bounded_preview(value, limit, lambda item: item)


def _bounded_preview(value: object, limit: int,
                     envelope: Callable[[object], object]) -> object:
    raw = canonical(value)
    if _size(envelope(value)) <= limit:
        return value
    result = {"truncated": True, "json_preview": "", "sha256": digest(value),
              "full_bytes": len(raw.encode("utf-8"))}
    if _size(envelope(result)) > limit:
        raise ContextOverflow("observation identity and recovery metadata exceed observation_bytes")
    # JSON quoting can expand text even after a UTF-8 byte slice. Fit the actual
    # replacement, including its receipt envelope, rather than a fixed fraction.
    low, high = 0, len(raw)
    while low < high:
        middle = (low + high + 1) // 2
        if _size(envelope({**result, "json_preview": raw[:middle]})) <= limit:
            low = middle
        else:
            high = middle - 1
    return {**result, "json_preview": raw[:low]}


def _observation(value: object, limit: int,
                 envelope: Callable[[object], object]) -> object:
    if _size(envelope(value)) <= limit:
        return value
    try:
        page = fit_observation_page(value, limit, envelope)
    except ValueError:
        # Never advertise a source cursor when no source character fits. The
        # surrounding observation retains the original read_receipt identity.
        marker = {"truncated": True, "sha256": digest(value), "full_bytes": _size(value),
                  "reason": "page_metadata_exceeds_observation_budget"}
        if _size(envelope(marker)) > limit:
            raise ContextOverflow("page identity and recovery metadata exceed observation_bytes") from None
        return marker
    if page is not None:
        return page
    return _bounded_preview(value, limit, envelope)


def _turn_observation(result: Json, limit: int) -> Json:
    envelope = lambda data: {**result, "data": data}
    return envelope(_observation(result["data"], limit, envelope))


def source(task: Task, policy: Policy, state: Json, skills: list[Json], *,
           agent: Json | None = None, goal_progress: Json | None = None) -> Json:
    """Acquire bounded candidates without deciding which exchanges fit the prompt."""
    task_view = task.model_dump(mode="json", exclude={"seed_files"})
    task_view["seed_manifest"] = {
        path: {"sha256": hashlib.sha256(text.encode()).hexdigest(), "bytes": len(text.encode())}
        for path, text in task.seed_files.items()
    }
    actions = [a for a in state["actions"].values() if a["action"]["task"] == task.id
               and not a["action"]["tool"].startswith("__") and a.get("terminal")]
    latest_action_id = actions[-1]["action"]["id"] if actions else None
    proposals = native_proposals(task.id, state)
    owners = {item["action"]["id"] for item in proposals}
    # Classification is cheap and covers omitted turns, too. Only retained
    # exchanges need parsing, hashing, and byte-bounded result projection.
    native_ids = {item["action"]["id"] for item in actions
                  if any(parent in owners for parent in item["action"].get("parents", ()))}
    turns = project_turns(task.id, state, _preview, policy.observation_bytes,
                          max_items=policy.history_items, observation=_turn_observation)
    actions = [item for item in actions if item["action"]["id"] not in native_ids]
    selected = actions[-policy.history_items:]
    history = []
    for item in selected:
        action = item["action"]
        outcome = item["outcome"]
        receipt = {"action_id": action["id"], "sha256": digest(item), "read_tool": "read_receipt"}
        envelope = lambda data: {"outcome": {**outcome, "data": data}, "receipt": receipt}
        history.append({
            "action": {**action, "arguments": _preview(action["arguments"], policy.observation_bytes)},
            **envelope(_observation(outcome["data"], policy.observation_bytes, envelope)),
        })
    context: Json = {"task": task_view, "skills": skills, "policy": policy.model_dump(mode="json"),
               "budget": {k: state[k] for k in ("limit", "spent", "reserved")}, "history": history,
               "projection": {"version": "kernelagent.context/v2",
                              "history_omitted": len(actions) - len(history),
                              "authority": "journal", "context_byte_limit": policy.context_bytes,
                              "latest_action_id": latest_action_id}}
    if goal_progress is not None:
        context["goal_progress"] = goal_progress
    if turns:
        context["turns"] = turns[-policy.history_items:]
        context["projection"]["latest_model_action_id"] = turns[-1]["proposal_id"]
        context["projection"]["version"] = "kernelcat.context/v3"
        context["projection"]["turns_omitted"] = len(proposals) - len(context["turns"])
    batches = [batch for batch in input_batches(task.id, state) if batch["messages"]]
    if batches:
        context["inputs"] = batches
    if agent is not None:
        context["agent"] = agent
    if "runtime_knowledge" in state:
        context["knowledge"] = state["runtime_knowledge"]
    return context


def project(task: Task, policy: Policy, state: Json, skills: list[Json], *,
            agent: Json | None = None, goal_progress: Json | None = None) -> Json:
    """Legacy embedded projection; the product ContextCompiler owns prompt fitting."""
    context = source(task, policy, state, skills, agent=agent, goal_progress=goal_progress)
    history = context["history"]
    while True:
        if _size(context) <= policy.context_bytes:
            return context
        if not history and len(context.get("inputs", [])) > 1:
            context["inputs"].pop(0)
            continue
        if not history and context.get("turns"):
            context["turns"].pop(0)  # Drop the whole exchange, never half a tool batch.
            context["projection"]["turns_omitted"] += 1
            continue
        if not history:
            raise ContextOverflow("task contract, policy, agent profile and skill catalogue exceed context_bytes")
        history.pop(0)
        context["projection"]["history_omitted"] += 1
