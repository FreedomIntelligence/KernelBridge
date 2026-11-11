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

    def __init__(self, settings=None) -> None:
        self.settings = settings or {"max_per_turn": 4, "max_total": None, "max_steps": 6,
                                     "tool_limit": 16, "timeout": 120}
        self.denial = None
        self.spec = ToolSpec(name="delegate", description=(
            "Use only when the user or applicable project instructions explicitly request delegation. "
            "Ask an upstream pi-subagents foreground child (kernel-scout or kernel-reviewer) to analyze supplied evidence. "
            "Mode analysis is text-only; read_only can inspect candidate files, skills and receipts. "
            "No child may write, run shell/devices or delegate. Answers are advisory, not validation."), credits=1,
            parameters={"type": "object", "additionalProperties": False,
                "properties": {"agent": {"type": "string", "enum": ["kernel-scout", "kernel-reviewer"]},
                    "task": {"type": "string", "minLength": 1, "maxLength": 32768},
                    "mode": {"type": "string", "enum": ["analysis", "read_only"], "default": "analysis"},
                    "max_steps": {"type": "integer", "minimum": 1, "maximum": self.settings["max_steps"]}},
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
            if self.denial:
                return Outcome(status="failed", data={"error": self.denial})
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
