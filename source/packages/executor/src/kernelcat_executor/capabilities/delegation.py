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
        self.settings = settings or {"max_per_turn": 4, "max_total": None, "max_steps": 20,
                                     "tool_limit": 64, "timeout": 600}
        self.denial = None
        guidance = (
            "Use proactively for independent codebase exploration or a focused second review when delegation saves main-context work. "
            "Respect user/project instructions that disable delegation; handle trivial lookups directly. "
            if self.settings.get("policy", "auto") == "auto" else
            "Use only when the user or applicable project instructions explicitly request delegation. "
        )
        self.spec = ToolSpec(name="delegate", description=(
            guidance + "Launch a child agent with an independent context and return its findings. "
            "Always give a short description (3-6 words) that the user sees while the child runs. "
            "kernel-scout discovers files, searches code and traces implementations; kernel-reviewer checks a focused change or claim against evidence. "
            "Supply a self-contained task, relevant paths/constraints and expected output; children do not inherit the conversation. "
            "Default mode read_only can list/search/read project files, skills and receipts; analysis uses supplied text only. "
            "Split independent questions into separate calls, then synthesize their results. Children run sequentially in the foreground. "
            "No child may write, run shell/devices or delegate. Answers are advisory, not validation."), credits=1,
            parameters={"type": "object", "additionalProperties": False,
                "properties": {"agent": {"type": "string", "enum": ["kernel-scout", "kernel-reviewer"]},
                    "description": {"type": "string", "minLength": 1, "maxLength": 80,
                                    "description": "Short user-visible label for this child, e.g. 'Trace build flow'."},
                    "task": {"type": "string", "minLength": 1, "maxLength": 32768},
                    "mode": {"type": "string", "enum": ["analysis", "read_only"], "default": "read_only"},
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
