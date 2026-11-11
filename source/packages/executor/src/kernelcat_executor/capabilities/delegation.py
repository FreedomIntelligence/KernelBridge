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
    # Gateway batches stay sequential; concurrent children use the plugin
    # protocol, one rendezvous per delegate action.
    execution_mode = "sequential"

    BUILTIN_AGENTS = (
        {"name": "kernel-scout", "description": "discovers files, searches code and traces implementations", "tools": "read"},
        {"name": "kernel-reviewer", "description": "checks a focused change or claim against original evidence", "tools": "read"},
    )

    def __init__(self, settings=None) -> None:
        self.settings = {"max_concurrent": 6, "max_per_turn": None, "max_total": None, "max_steps": None,
                         "tool_limit": None, "timeout": None, "background": True, **(settings or {})}
        self.agents = {agent["name"]: dict(agent) for agent in (*self.BUILTIN_AGENTS, *self.settings.get("agents", ()))}
        self.spec = self._specification()
        self.lock = Lock()
        self.denials: dict[str, str | None] = {}
        self.started: dict[str, Event] = {}
        self.futures: dict[str, Future[Outcome]] = {}

    def arm(self, identity: str, denial: str | None = None) -> Event:
        with self.lock:
            if identity in self.futures:
                raise Conflict("delegation already has a host rendezvous")
            self.futures[identity] = Future()
            self.started[identity] = Event()
            self.denials[identity] = denial
            return self.started[identity]

    def execute(self, action: Action) -> Outcome:
        with self.lock:
            future = self.futures.get(action.id)
            if future is None:
                raise Conflict("delegation has no host rendezvous")
            denial = self.denials.get(action.id)
            if not denial:
                # A denied call settles as cached; it must never hand Pi a permit.
                self.started[action.id].set()
        if denial:
            return Outcome(status="failed", data={"error": denial[1], "reason": denial[0]})
        return future.result()

    def resolve(self, identity: str, outcome: Outcome) -> None:
        with self.lock:
            future = self.futures.get(identity)
            if future is None or future.done():
                raise Conflict("delegation is not awaiting settlement")
            future.set_result(outcome)

    def done(self, identity: str) -> bool:
        with self.lock:
            future = self.futures.get(identity)
            return future is None or future.done()

    def clear(self, identity: str) -> None:
        with self.lock:
            future = self.futures.get(identity)
            if future is not None and not future.done():
                raise Conflict("cannot discard an unsettled delegation")
            self.futures.pop(identity, None)
            self.started.pop(identity, None)
            self.denials.pop(identity, None)

    def add_agents(self, agents) -> None:
        """Project/user definitions and project-only built-ins; regenerates the model-visible spec."""
        for agent in agents:
            self.agents[agent["name"]] = dict(agent)
        self.spec = self._specification()

    def _specification(self) -> ToolSpec:
        guidance = (
            "Use proactively for independent codebase exploration or a focused second review when delegation saves main-context work. "
            "Respect user/project instructions that disable delegation; handle trivial lookups directly. "
            if self.settings.get("policy", "auto") == "auto" else
            "Use only when the user or applicable project instructions explicitly request delegation. "
        )
        background = self.settings.get("background", True)
        kinds = {"read": "read-only", "test": "can also run commands", "write": "edits an isolated Git worktree branch"}
        catalogue = "; ".join(f"{name} ({kinds[agent.get('tools', 'read')]}): {agent['description']}"
                              for name, agent in self.agents.items())
        writes = any(agent.get("tools") == "write" for agent in self.agents.values())
        return ToolSpec(name="delegate", description=(
            guidance + "Launch a child agent with an independent context and return its findings. "
            "Always give a short description (3-6 words) that the user sees while the child runs. "
            f"Agents: {catalogue}. "
            "Supply a self-contained task, relevant paths/constraints and expected output; children do not inherit the conversation. "
            "Read agents can list/search/read project files, skills and receipts (mode analysis uses supplied text only). "
            + ("A worktree agent returns its branch and diff; review it and apply it yourself (apply_hint) only if it is correct. " if writes else "")
            + "Split independent questions into separate calls in the same reply; those children run concurrently. Then synthesize their results. "
            + ("Set background=true only for long work the user need not wait for: the call returns at once and the result arrives later "
               "as a [子代理结果] message; agents_status shows progress and stop_agent cancels. " if background else "")
            + "Children cannot spawn further children. Answers are advisory, not validation."), credits=1,
            parameters={"type": "object", "additionalProperties": False,
                "properties": {"agent": {"type": "string", "enum": list(self.agents)},
                    "description": {"type": "string", "minLength": 1, "maxLength": 80,
                                    "description": "Short user-visible label for this child, e.g. 'Trace build flow'."},
                    "task": {"type": "string", "minLength": 1, "maxLength": 32768},
                    "mode": {"type": "string", "enum": ["analysis", "read_only"], "default": "read_only"},
                    "max_steps": {"type": "integer", "minimum": 1, "maximum": self.settings["max_steps"] or 256},
                    **({"background": {"type": "boolean", "default": False,
                        "description": "Run without blocking; the result is delivered later as a notification."}} if background else {})},
                "required": ["agent", "task"]})
