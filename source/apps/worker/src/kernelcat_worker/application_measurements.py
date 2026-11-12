"""Noise calibration and evidence projection from original paired RSI receipts."""
import math
import statistics

from kernelcat_executor.contracts.models import Conflict, digest
from kernelcat_executor.capabilities.rsi_artifacts import source_files
from kernelcat_executor.contracts.rsi import validate_rsi


def profile_version(host):
    return digest({"measurement_profile": host.task.contract.get("measurement_profile", {}),
                   "evaluator_sha256": host.evaluator["sha256"]})


def calibrate(host, connection):
    if host.owner.rsi is None:
        return
    version = profile_version(host)
    if any(item.get("operation") == "noise_calibration" and item.get("measurement_profile_version") == version
           for item in host.journal.state()["configurations"]):
        return
    rsi = host.owner.rsi
    files = source_files(rsi.workspace, rsi.config["source_paths"])
    identity = host.task.id + "/rsi/calibration/" + version
    request = {"schema_version": "kernelcat.rsi-adapter/v1", "operation": "confirm", "task_id": host.task.id,
        "experiment_id": identity, "unit_id": "baseline-noise", "baseline": files, "keep": files, "remove": files,
        "sample_namespace": identity}
    result = rsi.adapter.call(host.owner.gateway, host.task.id, identity, request)
    measured = validate_rsi(result["measurement"], "PairedMeasurement")
    left, right = measured["keep_log_scores"], measured["remove_log_scores"]
    if measured["keep_correct"] is not True or measured["remove_correct"] is not True or len(left) != len(right) or len(left) < 2:
        raise Conflict("baseline noise calibration lacks complete repeated correctness/timing")
    differences = [a - b for a, b in zip(left, right, strict=True)]
    threshold = abs(statistics.mean(differences)) + 3 * statistics.stdev(differences) / math.sqrt(len(differences))
    host.journal.command("configuration", {**connection.worker, "operation": "noise_calibration",
        "measurement_profile_version": version, "environment_id": measured["environment_id"],
        "protocol_id": measured["protocol_id"], "noise_threshold": threshold,
        "evidence_ref": identity, "raw_data_ref": measured["evidence_ids"], "baseline_source_sha256": measured["keep_digest"]})


def collect_paired(host):
    if host.owner.rsi is None:
        return
    state = host.journal.state()
    calibrations = [item for item in state["configurations"] if item.get("operation") == "noise_calibration"
                    and item.get("measurement_profile_version") == profile_version(host)]
    if not calibrations:
        return
    calibration = calibrations[-1]
    records = host.owner.rsi.records
    known = {item["evidence_ref"] for item in state["evidence"]}
    contexts = {row["action"]["id"]: row.get("application_context", {})
                for row in host.owner.gateway.store._events(host.owner.gateway.store.events()) if row["kind"] == "admitted"}
    for item in records.entries():
        record = records.decode(item)
        if record["kind"] != "acceptance" or record["record_id"] in known:
            continue
        value = record["value"]
        measurement = validate_rsi(value["confirmation"], "PairedMeasurement")
        identity = value["experiment_id"] + "/adapter/confirm/" + digest("confirmation")
        if contexts.get(identity, {}).get("task_version") != state["task_version"]:
            continue
        if (measurement["keep_correct"] is not True or measurement["remove_correct"] is not True
                or not measurement["independent_confirmation"] or not measurement["holdout_coverage"]
                or measurement["environment_id"] != calibration["environment_id"]
                or measurement["protocol_id"] != calibration["protocol_id"]):
            continue
        left, right = measurement["keep_log_scores"], measurement["remove_log_scores"]
        if len(left) != len(right) or len(left) < 2:
            continue
        delta = statistics.mean(b - a for a, b in zip(left, right, strict=True))
        # Only adapters with a declared alternating/ABBA protocol may qualify E3.
        protocol = measurement["protocol_id"]
        pair_method = "ABBA" if "abba" in protocol.lower() else "alternating" if ("alternating" in protocol or protocol == "cannbench-season1-sigmoid-20-full-output-v1") else None
        tier = "E3" if pair_method and delta > calibration["noise_threshold"] and value["request"]["accept"] else "E2"
        receipt = {"evidence_tier": tier, "source_sha256": measurement["remove_digest"],
            "parent_source_sha256": measurement["keep_digest"], "evaluator_sha256": host.evaluator["sha256"],
            "task_version": state["task_version"], "measurement_profile_version": profile_version(host),
            "environment_id": measurement["environment_id"], "correct": True, "full_suite": True,
            "evidence_ref": record["record_id"], "raw_data_ref": measurement["evidence_ids"],
            "score": statistics.mean(right), "score_unit": "mean negative log milliseconds",
            "paired_delta": delta, "noise_threshold": calibration["noise_threshold"], "pair_method": pair_method,
            "source_snapshot": value.get("source_snapshot"), "calibration_ref": calibration["evidence_ref"]}
        host.journal.command("evidence", receipt)
        requirement = state["task"].get("contract", {}).get("acceptance", {})
        if (host.config.application.complete_on_acceptance and state["goal"] and state["goal"]["status"] == "active" and requirement.get("evidence_tier") == tier
                and (requirement.get("target") is None or receipt["score"] >= requirement["target"])):
            host.journal.command("goal_outcome", {"status": "completed", "evidence_ref": record["record_id"]})
