"""Trusted boundary observer; an agent cannot call an install/activate tool."""
from __future__ import annotations

from kernelcat_executor.contracts.models import Action, Conflict, InDoubt, Outcome, digest
from kernelcat_executor.contracts.ports import ProfileStore
from kernelcat_executor.operations.gateway import Gateway


class PromoteOnFinish:
    def after_action(self, gateway: Gateway, action: Action, outcome: Outcome) -> None:
        if action.tool != "finish" or outcome.status != "completed":
            return
        store = gateway.store
        if not isinstance(store, ProfileStore):
            raise TypeError("agent evolution requires a ProfileStore")
        state = store.snapshot()
        # Exactly one accepted candidate is considered per finished task. A stale
        # candidate is recorded as failed; it is never rebased without validation.
        for item in reversed(list(state["actions"].values())):
            if (item["action"]["task"] != action.task
                    or item["action"]["tool"] != "validate_improvement"
                    or not item.get("terminal")
                    or item["outcome"]["status"] != "completed"
                    or item["outcome"]["data"].get("accepted") is not True):
                continue
            validation_id = item["action"]["id"]
            proposal_id = item["outcome"]["data"]["proposal_id"]
            activation = Action(id=action.task + "/activate/" + digest(validation_id),
                task=action.task, tool="__activate_agent_profile__",
                arguments={"proposal_id": proposal_id, "validation_id": validation_id,
                           "finish_id": action.id}, parents=(action.id, validation_id))

            def install() -> Outcome:
                try:
                    revision = store.activate_profile(proposal_id, validation_id, action.id)
                except Conflict as error:
                    return Outcome(status="failed", data={"reason": str(error)})
                return Outcome(data={"revision": revision.model_dump(mode="json"),
                                     "revision_digest": revision.fingerprint()})

            result = gateway.internal(activation, 1, install)
            if result.status == "unknown":
                raise InDoubt(activation.id)
            return
