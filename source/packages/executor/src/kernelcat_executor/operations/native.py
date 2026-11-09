"""A finite Host permit for one Pi native invocation and its admitted nested effects."""
from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from concurrent.futures import Future
from threading import Event, Thread
from typing import cast

from kernelcat_executor.contracts.harness import MCP_PREFIX, PI_READ_TOOLS, PI_WRITE_TOOLS, validate_native_result
from kernelcat_executor.contracts.models import (
    Action,
    Conflict,
    InDoubt,
    Json,
    Outcome,
    ToolSpec,
    canonical,
    digest,
)
from kernelcat_executor.generated.harness import NativeCallRequest, NativeSettleRequest
from kernelcat_executor.operations.gateway import Gateway


class NativeEffects:
    """The Gateway owns every admission; this object only binds a live invocation.

    Calls are deliberately serial. Lookup and duplicate comparison are O(1), with
    at most 64 retained call records. Store/guard costs remain at their owner.
    """

    def __init__(self, gateway: Gateway, action: Action, allowed: frozenset[str],
                 declarations: Mapping[str, Json], execute: Callable[[Action], Outcome],
                 observe: Callable[[Action, Outcome], None], cancelled: Callable[[], bool], *,
                 timeout: float = 60.0, clock: Callable[[], float] = time.monotonic) -> None:
        self.gateway, self.action, self.allowed = gateway, action, allowed
        self.execute, self.observe, self.cancelled = execute, observe, cancelled
        self.clock, self.deadline = clock, clock() + timeout
        self.declaration_digest = digest(declarations[action.tool])
        self.declaration = dict(declarations[action.tool])
        self.calls: list[tuple[str, Json]] = []
        self.resolution: Future[Outcome] = Future()
        self.finished: Future[Outcome] = Future()
        self.started = Event()
        self.thread: Thread | None = None
        self.settlement: str | None = None
        self.closed = False

    def _await_result(self) -> Outcome:
        self.started.set()
        return self.resolution.result()

    def _dispatch(self) -> None:
        try:
            if self.action.tool in PI_READ_TOOLS | PI_WRITE_TOOLS:
                result = self.gateway.execute_native(self.action,
                    ToolSpec(**self.declaration, credits=1), self._await_result)
            else:
                result = self.gateway.internal(self.action, 0, self._await_result)
            self.finished.set_result(result)
        except BaseException as error:
            self.finished.set_exception(error)

    def begin(self) -> Json:
        self.thread = Thread(target=self._dispatch, name="kernelcat-native-effect", daemon=True)
        self.thread.start()
        while not self.started.wait(0.02):
            if self.finished.done():
                outcome = self.finished.result()
                self.closed = True
                self.thread.join()
                if outcome.status == "unknown":
                    raise InDoubt(self.action.id)
                return {"cached": True, "action_id": self.action.id, "outcome": outcome.model_dump(mode="json")}
            if self.cancelled():
                self.close()
                raise Conflict("native invocation cancelled before its permit was delivered")
        return {"permit": self.action.id}

    def _live(self, permit: str) -> None:
        if permit != self.action.id or self.closed or self.resolution.done():
            raise Conflict("native call has no live invocation permit")
        if self.cancelled() or self.clock() >= self.deadline:
            raise Conflict("native invocation permit cancelled or expired")

    def _unresolved(self) -> None:
        for item in self.gateway.store.snapshot()["actions"].values():
            action = item["action"]
            if (action["task"] == self.action.task and action["id"] != self.action.id
                    and not item.get("terminal")):
                raise InDoubt(f"native invocation has an unresolved effect: {action['id']}")

    def call(self, request: NativeCallRequest) -> Json:
        self._live(request["permit"])
        self._unresolved()
        sequence, name = request["sequence"], request["name"]
        if name not in self.allowed:
            raise Conflict(f"native invocation requested an unavailable Gateway tool: {name}")
        self._direct_leaf(request)
        fingerprint = digest(request)
        if sequence < len(self.calls):
            original, receipt = self.calls[sequence]
            if fingerprint != original:
                raise Conflict("native call sequence reused with different inputs")
            return cast(Json, json.loads(canonical(receipt)))
        if sequence != len(self.calls) or sequence >= 64:
            raise Conflict("native call sequence is out of order or exceeds its bound")
        parents = self.action.parents
        if self.calls:
            parents = (*parents, self.calls[-1][1]["action_id"])
        action = Action(id=f"{self.action.id}/nested/{sequence}", task=self.action.task,
                        tool=name, arguments=dict(request["arguments"]), parents=parents)
        outcome = self.execute(action)
        receipt = {"action_id": action.id, "outcome": outcome.model_dump(mode="json")}
        self.calls.append((fingerprint, receipt))
        if outcome.status == "unknown":
            raise InDoubt(action.id)
        self.observe(action, outcome)
        return cast(Json, json.loads(canonical(receipt)))

    def _direct_leaf(self, request: NativeCallRequest) -> None:
        if not self.action.tool.startswith(MCP_PREFIX):
            return
        expected = self.action.tool[len(MCP_PREFIX):]
        if (request["sequence"] != 0 or request["name"] != expected
                or canonical(request["arguments"]) != canonical(self.action.arguments)):
            raise Conflict("native MCP call differs from its admitted direct invocation")

    def settle(self, request: NativeSettleRequest) -> Json:
        encoded = canonical(request)
        if self.settlement is not None:
            if encoded != self.settlement:
                raise Conflict("native invocation received conflicting settlement")
            return self._receipt()
        self._live(request["permit"])
        self._unresolved()
        validate_native_result(dict(request["result"]), request["status"])
        outcome = Outcome(status=request["status"], data={
            "result": request["result"], "native_declaration_digest": self.declaration_digest,
            "nested_effects": [receipt["action_id"] for _, receipt in self.calls],
            "adapter": "pi@1.5.0", "promoted": False,
        })
        self.settlement = encoded
        self.resolution.set_result(outcome)
        self.closed = True
        assert self.thread is not None
        self.thread.join()
        return self._receipt()

    def _receipt(self) -> Json:
        outcome = self.finished.result()
        if outcome.status == "unknown":
            raise InDoubt(self.action.id)
        return {"action_id": self.action.id, "outcome": outcome.model_dump(mode="json")}

    def close(self) -> None:
        if self.thread is None:
            return
        if not self.resolution.done() and not self.finished.done():
            self.resolution.set_result(Outcome(status="unknown", data={
                "reason": "Pi native invocation interrupted", "adapter": "pi@1.5.0",
                "nested_effects": [receipt["action_id"] for _, receipt in self.calls],
            }))
        self.closed = True
        self.thread.join()
        self.finished.result()
