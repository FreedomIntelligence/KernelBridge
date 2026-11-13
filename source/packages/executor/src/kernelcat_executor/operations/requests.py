"""Prepared-request logging and read-only reconstruction; never replay network effects."""
from __future__ import annotations

import json

from kernelcat_executor.capabilities.provider_evidence import ProviderEvidence, capture_provider_evidence
from kernelcat_executor.contracts.messages import AssistantTurn
from kernelcat_executor.contracts.models import (
    Action,
    Conflict,
    Json,
    Outcome,
    Policy,
    Reply,
    ToolSpec,
    canonical,
    digest,
)
from kernelcat_executor.contracts.ports import HarnessProposer, PreparedProposer, Proposer, Store
from kernelcat_executor.contracts.requests import ModelRequest
from kernelcat_executor.contracts.runtime import ContextAdmission
from kernelcat_executor.generated.context import ContextBudget
from kernelcat_executor.generated.harness import HarnessInput
from kernelcat_executor.operations.gateway import Gateway


def recorded_request(store: Store, action_id: str) -> ModelRequest:
    """Read the actual admission record, not a re-rendered current prompt."""
    item = store.snapshot()["actions"].get(action_id)
    if not item or item["action"]["tool"] != "__propose__":
        raise Conflict("model request admission not found")
    arguments = item["action"]["arguments"]
    raw = arguments.get("request")
    if raw is None:
        raise Conflict("this proposer/legacy receipt has no recorded wire request")
    request = ModelRequest.model_validate_json(canonical(raw))
    if request.fingerprint() != arguments.get("request_digest"):
        raise Conflict("model request digest mismatch")
    return request


def recorded_provider_response(store: Store, action_id: str) -> Json:
    """Read a failed call's original observation without invoking its provider.

    Redacted records and assembled-versus-wire scope remain explicit. This API
    does not claim that decoding again will succeed or authorize replayed tools.
    """
    request = recorded_request(store, action_id)
    admitted = store.snapshot()["actions"][action_id]
    reference = admitted.get("outcome", {}).get("data", {}).get("failure_details", {}).get("provider_response", {})
    if reference.get("status") != "recorded":
        raise Conflict("this model failure has no retained provider response")
    evidence = ProviderEvidence(store, admitted, request)
    if evidence.artifacts is None:
        raise Conflict("model response artifact storage is unavailable")
    raw = evidence.artifacts.read(reference)
    record = json.loads(raw)
    if (record.get("schema_version") != "kernelbridge.provider-response/v1"
            or any(record.get(key) != value for key, value in evidence.binding.items())
            or record.get("capture_scope") != "assembled_provider_response"
            or record.get("wire_complete") is not False
            or record.get("redacted") != reference.get("redacted")):
        raise Conflict("retained provider response differs from its original admission")
    return record


def recorded_harness_tools(store: Store, action_id: str) -> dict[str, Json]:
    """Recover declarations from the admitted wire, including on response replay."""
    arguments = store.snapshot()["actions"][action_id]["action"]["arguments"]
    if arguments.get("harness_adapter") is None:
        return {}
    request = recorded_request(store, action_id)
    tools = request.payload().get("tools", [])
    if request.provider == "openai":
        declarations = [tool["function"] for tool in tools]
    else:
        declarations = [{"name": tool["name"], "description": tool["description"],
                         "parameters": tool["input_schema"]} for tool in tools]
    if digest(declarations) != arguments["harness_tools_digest"]:
        raise Conflict("admitted native declarations do not match their recorded digest")
    return {tool["name"]: tool for tool in declarations}


def propose(gateway: Gateway, proposer: Proposer, action_id: str, task_id: str,
            route: str, context: Json, tools: tuple[ToolSpec, ...],
            parents: tuple[str, ...], *, expected_budget: ContextBudget | None = None,
            harness: HarnessInput | None = None,
            adoption: ContextAdmission | None = None,
            native_checkpoint_required: bool = False) -> Outcome:
    """Prepare without I/O, admit durably, then send exactly the logged bytes.

    Legacy/scripted Proposers still work but only claim adapter-input replay, not
    wire reconstruction. Native adapters implement PreparedProposer explicitly.
    """
    detached_context = json.loads(canonical(context))
    specs = tuple(ToolSpec.model_validate_json(canonical(t)) for t in tools)
    arguments: Json = {"route": route, "context": detached_context,
                       "context_digest": digest(context),
                       "tool_specs": [t.model_dump(mode="json") for t in specs],
                       "tools_digest": digest([t.model_dump(mode="json") for t in specs])}
    request: ModelRequest | None = None
    if harness is not None and not isinstance(proposer, HarnessProposer):
        raise Conflict("configured model transport has no effective Harness input capability")
    if isinstance(proposer, PreparedProposer) or harness is not None:
        if harness is not None:
            assert isinstance(proposer, HarnessProposer)
            request = proposer.prepare_harness(harness)
        else:
            assert isinstance(proposer, PreparedProposer)
            request = proposer.prepare(json.loads(canonical(context)), specs)
        request = ModelRequest.model_validate_json(canonical(request))
        # The body already contains system, context and schemas. Do not duplicate
        # their large strings in the journal's admission record.
        arguments = {"route": route, "context_digest": digest(context),
                     "tools_digest": digest([t.model_dump(mode="json") for t in specs]),
                     "request": request.model_dump(mode="json"),
                     "request_digest": request.fingerprint(), "replay_level": "wire"}
        if harness is not None:
            arguments.update(harness_adapter=harness["adapter"],
                             harness_digest=digest(harness),
                             harness_tools_digest=digest(harness["tools"]))
    else:
        arguments["replay_level"] = "adapter_input_only"
    if "rsi" in context:
        arguments["rsi_context"] = {"ku_ids": [item["ku_id"] for item in context["rsi"]],
                                    "digest": digest(context["rsi"])}
    if native_checkpoint_required:
        arguments["native_checkpoint_required"] = True
    action = Action(id=action_id, task=task_id, tool="__propose__",
                    arguments=arguments, parents=parents)

    def invoke() -> Outcome:
        reply: Reply | AssistantTurn
        if isinstance(proposer, PreparedProposer) or harness is not None:
            logged = recorded_request(gateway.store, action.id)
            if request is None or logged.fingerprint() != request.fingerprint():
                raise Conflict("prepared model request diverged from durable admission")
            admitted = gateway.store.snapshot()["actions"][action.id]
            with capture_provider_evidence(ProviderEvidence(gateway.store, admitted, logged)):
                if harness is not None:
                    assert isinstance(proposer, HarnessProposer)
                    reply = proposer.invoke_harness(logged)
                else:
                    assert isinstance(proposer, PreparedProposer)
                    reply = proposer.invoke(logged)
        else:
            # Fresh inputs prevent a mutable custom adapter from changing the receipt.
            fresh_context = json.loads(canonical(detached_context))
            reply = proposer.propose(fresh_context, tuple(
                ToolSpec.model_validate_json(canonical(t)) for t in specs))
        return Outcome(data=reply.model_dump(mode="python"))

    expected = Policy.model_validate_json(canonical(context["policy"]))
    return gateway.internal(action, 1, invoke, expected_policy=expected,
                            expected_budget=expected_budget, adoption=adoption)
