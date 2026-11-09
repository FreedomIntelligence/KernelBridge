"""A sealed foreground Pi delegation effect, not another agent implementation."""
from __future__ import annotations

from concurrent.futures import Future
from threading import Event, Lock

from kernelcat_executor.contracts.models import Action, Conflict, Outcome, ToolSpec


class SubagentEffect:
    """Gateway starts the action before Pi is allowed to invoke the plugin.

    The process owner must settle or abandon the rendezvous on every exit path.
    An interrupted child becomes unknown, never a fabricated successful result.
    """
    execution_mode = "sequential"

    def __init__(self) -> None:
        self.spec = ToolSpec(name="delegate", description=(
            "Ask a foreground Pi subagent to analyze supplied evidence. "
            "Mode analysis is text-only; read_only can inspect candidate files, skills and receipts. "
            "No child may write, run shell/devices or delegate. Answers are advisory, not validation."), credits=1,
            parameters={"type": "object", "additionalProperties": False,
                "properties": {"agent": {"type": "string", "enum": ["kernel-scout", "kernel-reviewer"]},
                    "task": {"type": "string", "minLength": 1, "maxLength": 32768},
                    "mode": {"type": "string", "enum": ["analysis", "read_only"], "default": "analysis"},
                    "max_steps": {"type": "integer", "minimum": 1, "maximum": 8, "default": 6}},
                "required": ["agent", "task"]})
        self.lock = Lock()
        self.started = Event()
        self.identity: str | None = None
        self.future: Future[Outcome] | None = None

    def arm(self, identity: str) -> None:
        with self.lock:
            if self.future is not None:
                raise Conflict("one foreground delegation at a time")
            self.identity = identity
            self.future = Future()
            self.started.clear()

    def execute(self, action: Action) -> Outcome:
        with self.lock:
            if self.future is None or action.id != self.identity:
                raise Conflict("delegation has no host rendezvous")
            future = self.future
            self.started.set()
        return future.result()

    def resolve(self, outcome: Outcome) -> None:
        with self.lock:
            if self.future is None or self.future.done():
                raise Conflict("delegation is not awaiting settlement")
            self.future.set_result(outcome)

    def clear(self) -> None:
        with self.lock:
            if self.future is not None and not self.future.done():
                raise Conflict("cannot discard an unsettled delegation")
            self.future, self.identity = None, None
            self.started.clear()
