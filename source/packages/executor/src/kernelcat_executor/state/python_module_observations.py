"""Pure original-Journal observation projection; no filesystem work or second owner."""
from __future__ import annotations

from collections.abc import Iterable
from typing import cast

from kernelcat_executor.contracts.knowledge_fences import TRIAL_PREPARED_EVENT
from kernelcat_executor.contracts.models import Conflict, Json, canonical, digest
from kernelcat_executor.contracts.python_module_observation import (
    EVENT_KIND,
    make_observation,
    stable_module_identity,
    validate_observation,
)
from kernelcat_executor.contracts.version_lock import validate_version_lock
from kernelcat_executor.generated.python_module_observation import (
    PythonModuleObservationBinding,
    PythonModuleObservationReceipt,
    PythonModuleObservationRecord,
)
from kernelcat_executor.generated.python_modules import ExpectedPythonModules, PythonModuleReport


def _version_identity(
    entry: Json, validated_locks: dict[str, tuple[str, str]] | None = None,
) -> tuple[str, str]:
    key = canonical(entry["lock"]) if validated_locks is not None else None
    validated = validated_locks.get(key) if validated_locks is not None and key is not None else None
    if validated is None:
        lock = validate_version_lock(entry["lock"])
        validated = ("sha256:" + digest(lock), lock["runtime_inputs_digest"])
        if validated_locks is not None and key is not None:
            validated_locks[key] = validated
    identity, inputs_digest = validated
    # Identical lock bytes do not establish any other owner's supplied digest.
    if entry["version_lock_digest"] != identity:
        raise Conflict("module observation original VersionLock evidence digest differs")
    return identity, inputs_digest


def campaign_binding(rows: list[Json]) -> PythonModuleObservationBinding:
    original = next((row for row in rows if row["kind"] == "runtime"), None)
    if original is None:
        raise Conflict("module observation requires an existing runtime binding")
    return _campaign_binding(original)


def _campaign_binding(
    original: Json, validated_locks: dict[str, tuple[str, str]] | None = None,
) -> PythonModuleObservationBinding:
    if "version_lock" not in original:
        return {"kind": "fingerprint", "runtime_fingerprint": original["fingerprint"]}
    version_digest, inputs_digest = _version_identity(
        {"lock": original["version_lock"], "version_lock_digest": original["version_lock_digest"]},
        validated_locks)
    if (inputs_digest != "sha256:" + digest(original["inputs"])
            or original["fingerprint"] != digest(
                {"inputs": original["inputs"], "version_lock": original["version_lock"]})):
        raise Conflict("module observation original runtime binding differs")
    return {"kind": "structured", "runtime_fingerprint": original["fingerprint"],
            "original_version_lock_digest": version_digest, "runtime_inputs_digest": inputs_digest}


def managed_binding(account: Json, preparation: Json | None) -> PythonModuleObservationBinding:
    return _managed_binding(account, preparation)


def _managed_binding(
    account: Json, preparation: Json | None,
    validated_locks: dict[str, tuple[str, str]] | None = None,
) -> PythonModuleObservationBinding:
    authority = account["authority_digest"]
    if preparation is None:
        return {"kind": "account", "account_authority_digest": authority}
    prepared = preparation["prepared"]
    if prepared["schema_version"] == "kernelcat.prepared-run/v1":
        identity = prepared["intent_id"]
        run_id = preparation["manifest"]["run_id"]
    elif prepared["schema_version"] == "kernelcat.prepared-trial/v1":
        identity = prepared["request"]["intent_id"]
        run_id = prepared["request"]["expected"]["run_id"]
    else:
        raise Conflict("module observation has an unsupported preparation format")
    version_digest, inputs_digest = _version_identity(preparation["version_lock"], validated_locks)
    return {"kind": "managed", "account_authority_digest": authority,
            "run_id": run_id, "preparation_intent_id": identity,
            "original_version_lock_digest": version_digest, "runtime_inputs_digest": inputs_digest}


def _receipt(record: PythonModuleObservationRecord, sequence: int) -> PythonModuleObservationReceipt:
    return {"schema_version": "kernelcat.python-module-observation-receipt/v1",
            "event_sequence": sequence, "observation": record}


def apply_observation(state: Json, row: Json) -> None:
    try:
        record = validate_observation(row["observation"])
    except (KeyError, TypeError, ValueError) as error:
        raise Conflict("invalid retained Python module observation") from error
    observations = state.setdefault("python_module_observations", {})
    identity = record["observation_id"]
    if identity in observations:
        raise Conflict("duplicate physical Python module observation")
    admissions = state.setdefault("python_module_observation_bindings", {})
    binding_key = digest(record["binding"])
    stable_identity = stable_module_identity(record)
    if record["binding"]["kind"] != "account":
        previous = admissions.get(binding_key)
        if previous is not None and previous != stable_identity:
            raise Conflict("Python module expectation changed for the retained execution binding")
        admissions[binding_key] = stable_identity
    observations[identity] = _receipt(record, row["seq"])


def observation_event(
    state: Json, sequence: int, binding: PythonModuleObservationBinding,
    expected: ExpectedPythonModules, report: PythonModuleReport, *,
    owner_events: Iterable[Json],
) -> tuple[Json | None, PythonModuleObservationReceipt]:
    record = make_observation(binding, expected, report)
    previous = state.get("python_module_observations", {}).get(record["observation_id"])
    if previous is not None:
        return None, cast(PythonModuleObservationReceipt, previous)
    event = {"kind": EVENT_KIND, "observation": record}
    # The first observation enables owner-link checks that old unobserved rows skip.
    # Validate those checks as well as the new row before the atomic append.
    apply_observation(state, {**event, "seq": sequence})
    validate_owner_links(state, owner_events, managed=binding["kind"] in {"managed", "account"})
    return event, _receipt(record, sequence)


def validate_owner_links(state: Json, events: Iterable[Json], *, managed: bool) -> None:
    """O(E + P) owner indexing; lock validation is once per distinct canonical value.

    The local cache retains O(total distinct lock bytes) until this replay returns.
    Owner evidence digests, bindings and ordering remain independently checked.
    """
    observations = state.get("python_module_observations", {})
    if not observations:
        return
    allowed: dict[str, int] = {}
    validated_locks: dict[str, tuple[str, str]] = {}
    if managed:
        allowed[digest(managed_binding(state["account"], None))] = 0
    for event in events:
        binding = _event_binding(state, event, validated_locks, managed=managed)
        if binding is not None:
            allowed.setdefault(digest(binding), event["seq"])
    for receipt in observations.values():
        sequence = allowed.get(digest(receipt["observation"]["binding"]))
        if sequence is None or sequence >= receipt["event_sequence"]:
            raise Conflict("retained Python module observation has no matching original owner binding")


def _event_binding(
    state: Json, event: Json, validated_locks: dict[str, tuple[str, str]], *, managed: bool,
) -> PythonModuleObservationBinding | None:
    if not managed:
        return _campaign_binding(event, validated_locks) if event["kind"] == "runtime" else None
    if event["kind"] == "runtime_prepared":
        return _managed_binding(state["account"], event, validated_locks)
    if event["kind"] == TRIAL_PREPARED_EVENT:
        return _managed_binding(state["account"], event["record"], validated_locks)
    return None
