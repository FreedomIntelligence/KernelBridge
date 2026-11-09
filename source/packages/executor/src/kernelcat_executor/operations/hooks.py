"""Small execution seams; no plugin loader, policy compiler or workflow framework."""
from __future__ import annotations

from typing import Protocol

from kernelcat_executor.contracts.models import Action, Outcome
from kernelcat_executor.operations.gateway import Gateway


class OutcomeObserver(Protocol):
    """Trusted, explicitly composed code; effects must use Gateway and stable IDs.

    A completed receipt can be delivered again after resume. Observers must be
    idempotent by receipt identity; this is not an exactly-once callback promise.
    """
    def after_action(self, gateway: Gateway, action: Action, outcome: Outcome) -> None: ...
