"""Evidence grading operates on verified receipts, never on assistant prose."""
import math
from jsonschema import Draft202012Validator
from kernelcat_executor.generated.acceptance_evidence import SCHEMA

from kernelcat_executor.contracts.models import Conflict


def validate_evidence(receipt: dict, state: dict) -> None:
    if not Draft202012Validator(SCHEMA).is_valid(receipt):
        raise Conflict("acceptance evidence does not match the versioned contract")
    for key in ("source_sha256", "evaluator_sha256", "measurement_profile_version", "evidence_ref", "task_version"):
        if not receipt.get(key):
            raise Conflict("acceptance receipt missing " + key)
    if receipt["task_version"] != state["task_version"]:
        raise Conflict("acceptance receipt belongs to an old task version")
    expected = state.get("evaluator_sha256")
    if not expected or receipt["evaluator_sha256"] != expected:
        raise Conflict("acceptance evaluator differs from the frozen task")
    tier = receipt.get("evidence_tier")
    if tier not in {"E0", "E1", "E2", "E3", "E4"}:
        raise Conflict("unknown evidence tier")
    if tier in {"E1", "E2", "E3"} and not (receipt.get("correct") is True and receipt.get("full_suite") is True):
        raise Conflict("complete correctness evidence is required")
    if tier in {"E2", "E3"} and not receipt.get("raw_data_ref"):
        raise Conflict("performance receipt requires raw per-case measurements")
    if tier == "E3":
        delta, noise = receipt.get("paired_delta"), receipt.get("noise_threshold")
        if (isinstance(delta, bool) or isinstance(noise, bool) or
                not isinstance(delta, (int, float)) or not isinstance(noise, (int, float)) or
                not math.isfinite(delta) or not math.isfinite(noise) or noise < 0 or delta <= noise or
                not receipt.get("parent_source_sha256") or receipt.get("pair_method") not in {"ABBA", "alternating"}):
            raise Conflict("promotion requires a complete positive paired result above measured noise")
    if tier == "E4" and not receipt.get("platform_receipt"):
        raise Conflict("platform evidence requires the original platform receipt")
