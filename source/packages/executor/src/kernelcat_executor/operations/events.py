"""Ordered, awaited observation with detached payloads and a run-end barrier."""
from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from threading import RLock
from typing import cast

from kernelcat_executor.contracts.agent import AgentEvent, Listener
from kernelcat_executor.contracts.models import Conflict, canonical

_delivering: ContextVar[object | None] = ContextVar("kernelcat_event_delivery", default=None)


class EventBus:
    def __init__(self) -> None:
        self._listeners: list[Listener] = []
        self._lock = RLock()
        self.loop: asyncio.AbstractEventLoop | None = None

    @property
    def delivering(self) -> bool:
        return _delivering.get() is self

    def subscribe(self, listener: Listener) -> Callable[[], None]:
        if not callable(listener):
            raise TypeError("listener must be callable")
        with self._lock:
            self._listeners.append(listener)

        def unsubscribe() -> None:
            with self._lock:
                if listener in self._listeners:
                    self._listeners.remove(listener)
        return unsubscribe

    def emit(self, event: AgentEvent) -> None:
        with self._lock:
            listeners = tuple(self._listeners)
        if not listeners:
            return
        encoded = canonical(event)
        # The run owner serializes delivery. Do not hold the subscription lock
        # while awaiting a callback: callbacks may subscribe or unsubscribe.
        for listener in listeners:
            token = _delivering.set(self)
            try:
                result = listener(AgentEvent.model_validate_json(encoded))
                if inspect.isawaitable(result):
                    self._await(cast(Awaitable[object], result))
            finally:
                _delivering.reset(token)

    def _await(self, result: Awaitable[object]) -> None:
        async def deliver() -> None:
            token = _delivering.set(self)
            try:
                await result
            finally:
                _delivering.reset(token)

        if self.loop is not None:
            asyncio.run_coroutine_threadsafe(deliver(), self.loop).result()
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(deliver())
            return
        if inspect.iscoroutine(result):
            result.close()
        raise Conflict("use arun or aprompt to await subscribers from an event loop")
