"""Explicit, receipt-bound model retries; never reconcile uncertain tool effects."""
from __future__ import annotations

from dataclasses import dataclass

from kernelcat_executor.contracts.models import Conflict, InDoubt, Json, Policy, Task, digest


@dataclass(frozen=True)
class ModelRetry:
    failed_id: str
    next_id: str


def _successor(identity: str) -> str:
    # Separate from route IDs: a valid route may itself contain '/retry'.
    return "__model_retry__/" + digest(identity)


def latest_model_attempt(state: Json, base_id: str) -> tuple[str, Json | None]:
    """Follow durable retries, including when resuming without a retry flag.

    A retry has one deterministic successor. Repeating an operator command cannot
    authorize another attempt after that successor has already failed.
    """
    identity = base_id
    row = state["actions"].get(identity)
    while row is not None:
        if not row.get("terminal"):
            raise InDoubt(identity)
        next_id = _successor(identity)
        successor = state["actions"].get(next_id)
        if successor is None:
            break
        if (row["action"]["tool"] != "__propose__"
                or row["outcome"]["status"] != "failed"
                or successor["action"]["tool"] != "__propose__"
                or successor["action"]["task"] != row["action"]["task"]
                or successor["action"]["arguments"].get("route")
                != row["action"]["arguments"].get("route")
                or identity not in successor["action"].get("parents", ())):
            raise Conflict("invalid model retry chain")
        identity, row = next_id, successor
    return identity, row


def _root_attempt(state: Json, identity: str, task_id: str) -> str:
    # Check the hash links in both directions; do not guess a step from a route.
    seen: set[str] = set()
    while not identity.startswith(task_id + "/step/"):
        if identity in seen or not identity.startswith("__model_retry__/"):
            raise Conflict("invalid model retry lineage")
        seen.add(identity)
        row = state["actions"].get(identity)
        if row is None:
            raise Conflict("model retry parent is missing")
        parents = [p for p in row["action"].get("parents", ()) if _successor(p) == identity]
        if len(parents) != 1:
            raise Conflict("invalid model retry parent")
        identity = parents[0]
    return identity


def authorize_model_retry(state: Json, task: Task, policy: Policy,
                          failed_id: str) -> ModelRetry:
    """Validate operator intent under the task lease, before any new effect.

    Only known rate-limit/server failures are eligible. Unknown outcomes, transport
    timeouts, authentication and protocol failures are not authorization to retry.
    Recovery never edits a historical receipt.
    """
    for item in state["actions"].values():
        if item["action"]["task"] == task.id and not item.get("terminal"):
            raise InDoubt(item["action"]["id"])
    row = state["actions"].get(failed_id)
    if (row is None or row["action"]["task"] != task.id
            or row["action"]["tool"] != "__propose__"
            or not row.get("terminal") or row["outcome"]["status"] != "failed"):
        raise Conflict("retry requires a settled failed model receipt from this task")
    details = row["outcome"]["data"].get("failure_details", {})
    category = details.get("category") if isinstance(details, dict) else None
    if not isinstance(category, str) or category not in {"rate_limit", "server"}:
        raise Conflict("only known rate-limit/server model failures support explicit retry")
    route = row["action"]["arguments"].get("route")
    if not isinstance(route, str) or route not in policy.routes:
        raise Conflict("retry receipt has no configured model route")
    base_id = _root_attempt(state, failed_id, task.id)
    prefix = task.id + "/step/"
    if not base_id.startswith(prefix):
        raise Conflict("retry receipt has no runtime step identity")
    step_text, separator, _ = base_id[len(prefix):].partition("/model/")
    if (not separator or not step_text.isascii() or not step_text.isdecimal()
            or len(step_text) > 5):
        raise Conflict("retry receipt has no runtime step identity")
    step = int(step_text)
    if str(step) != step_text or (policy.max_steps is not None and step >= policy.max_steps):
        raise Conflict("retry receipt is outside the current step policy")
    step_id = f"{prefix}{step}"
    if base_id != f"{step_id}/model/{route}":
        raise Conflict("retry receipt route differs from its step identity")
    latest_id, _ = latest_model_attempt(state, base_id)
    next_id = _successor(failed_id)
    # Repeating an authorization reuses its existing successor, even if it failed.
    if next_id in state["actions"]:
        return ModelRetry(failed_id, next_id)
    if latest_id != failed_id:
        raise Conflict("retry must target the latest failed model attempt")
    call_id = step_id + "/call"
    if call_id in state["actions"] or call_id in state["requests"]:
        raise Conflict("step already has a tool request; retrying its model would fork execution")
    if step_id + "/end" in state["actions"]:
        raise Conflict("step already has a completed turn checkpoint")
    for candidate in policy.routes:
        _, sibling = latest_model_attempt(state, f"{step_id}/model/{candidate}")
        if sibling is not None and sibling["outcome"]["status"] == "completed":
            raise Conflict("step already has a completed model proposal")
    return ModelRetry(failed_id, next_id)
