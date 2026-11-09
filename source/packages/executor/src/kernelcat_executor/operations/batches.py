"""Bounded tool concurrency. The run owner alone delivers events and observations."""
from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from queue import SimpleQueue
from threading import Event

from kernelcat_executor.contracts.models import Action, BudgetExceeded, InDoubt, Outcome, canonical
from kernelcat_executor.operations.turns import BatchResult, BatchStatus

Prepared = Outcome | Callable[[], Outcome]
_executing: ContextVar[object | None] = ContextVar("kernelcat_batch_owner", default=None)


def running_in_batch(owner: object) -> bool:
    return _executing.get() is owner


def execute_parallel_calls(
    actions: tuple[Action, ...],
    prepare: Callable[[Action], Prepared],
    observe: Callable[[Action, Outcome], None],
    cancelled: Callable[[], bool],
    *,
    max_workers: int,
    owner: object | None = None,
    started: Callable[[Action], None],
    settled: Callable[[Action, Outcome], None],
    recorded: Callable[[Action, Outcome], None],
) -> BatchResult:
    """Preflight in source order, finalize in completion order, record in source order.

    Only admitted tool bodies run in worker threads. Callbacks run on the owner,
    including awaited listeners. Cancellation and errors stop new dispatch, then
    drain every in-flight call before returning or raising. Unstarted preparations
    hold no tool reservation and can be consumed on resume with their original IDs.
    """
    if type(max_workers) is not int or not 1 <= max_workers <= 32:
        raise ValueError("max_workers must be an integer between 1 and 32")
    if len({a.id for a in actions}) != len(actions):
        raise ValueError("batch action IDs must be unique")
    frozen = tuple(canonical(a) for a in actions)
    outcomes: dict[int, Outcome] = {}
    pending: list[tuple[int, Callable[[], Outcome]]] = []
    stop = Event()
    status: BatchStatus = "completed"
    failure: BaseException | None = None

    def action_at(index: int) -> Action:
        return Action.model_validate_json(frozen[index])

    def remember(error: BaseException) -> None:
        nonlocal failure
        # An uncertain effect must never be hidden by a failed UI callback.
        if failure is None or isinstance(error, InDoubt):
            failure = error
        stop.set()

    def finalize(index: int, candidate: Outcome) -> None:
        nonlocal status
        outcome = Outcome.model_validate_json(canonical(candidate))
        outcomes[index] = outcome
        if outcome.status == "unknown":
            remember(InDoubt(actions[index].id))
        elif outcome.data.get("rejected") and outcome.data.get("reason") == "budget":
            status = "budget_exhausted"
            stop.set()
        settled(action_at(index), Outcome.model_validate_json(canonical(outcome)))
        if (outcome.status != "unknown" and status == "completed" and failure is None
                and not stop.is_set() and not cancelled()):
            try:
                observe(action_at(index), Outcome.model_validate_json(canonical(outcome)))
            except BudgetExceeded:
                status = "budget_exhausted_during_update"
                stop.set()

    # No accepted tool body starts until every visited preflight has finished.
    for index in range(len(actions)):
        if cancelled() or stop.is_set():
            if status == "completed" and failure is None:
                status = "cancelled"
            break
        try:
            started(action_at(index))
            if cancelled():
                status = "cancelled"
                stop.set()
                break
            prepared = prepare(action_at(index))
            if isinstance(prepared, Outcome):
                finalize(index, prepared)
            else:
                pending.append((index, prepared))
        except BudgetExceeded:
            status = "budget_exhausted"
            stop.set()
        except BaseException as error:
            remember(error)

    completed: SimpleQueue[tuple[int, Outcome | None, BaseException | None]] = SimpleQueue()

    def invoke(index: int, function: Callable[[], Outcome]) -> None:
        result: Outcome | None = None
        error: BaseException | None = None
        token = _executing.set(owner)
        try:
            if not stop.is_set():
                result = Outcome.model_validate_json(canonical(function()))
                if (result.status == "unknown" or
                        (result.data.get("rejected") and result.data.get("reason") == "budget")):
                    stop.set()
        except BaseException as caught:
            error = caught
            stop.set()
        finally:
            # A FIFO, rather than iteration over a set of completed futures,
            # preserves completion order even when the owner is in a subscriber.
            _executing.reset(token)
            completed.put((index, result, error))

    if pending and failure is None and status == "completed" and not cancelled():
        # At most max_workers submissions are outstanding. There is no unbounded
        # executor queue, persistent pool, or thread per proposed tool call.
        with ThreadPoolExecutor(max_workers=min(max_workers, len(pending)),
                                thread_name_prefix="kernelcat-tool") as pool:
            cursor = active = 0
            while active or (cursor < len(pending) and not stop.is_set()):
                if cancelled():
                    if status == "completed":
                        status = "cancelled"
                    stop.set()
                while (active < max_workers and cursor < len(pending)
                       and not stop.is_set()):
                    index, function = pending[cursor]
                    try:
                        pool.submit(invoke, index, function)
                    except BaseException as error:
                        remember(error)
                        break
                    cursor += 1
                    active += 1
                if not active:
                    break
                index, outcome, worker_error = completed.get()
                active -= 1
                if worker_error is not None:
                    if isinstance(worker_error, BudgetExceeded):
                        status = "budget_exhausted"
                    else:
                        remember(worker_error)
                if outcome is not None:
                    try:
                        finalize(index, outcome)
                    except BaseException as error:
                        remember(error)
    elif status == "completed" and cancelled():
        status = "cancelled"

    # Completed siblings remain real results after cancellation or an unknown
    # outcome. Missing results are never fabricated or treated as successes.
    for index in sorted(outcomes):
        outcome = outcomes[index]
        if outcome.status != "unknown":
            try:
                recorded(action_at(index), Outcome.model_validate_json(canonical(outcome)))
            except BaseException as error:
                remember(error)
                break
    if failure is not None:
        raise failure
    if status == "completed" and len(outcomes) != len(actions):
        status = "cancelled"
    results = tuple(outcomes[i] for i in sorted(outcomes))
    terminate = (status == "completed" and bool(results)
                 and all(o.data.get("terminate") is True for o in results))
    return BatchResult(status, results, terminate)
