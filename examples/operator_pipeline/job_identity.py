"""Lossless job-handle projections; durable receipts are never rewritten."""
import json
import re


def wire_handle(handle):
    """Encode exact Python nanoseconds before they reach a JSON number consumer.

    Non-timestamp handle fields keep their original values and types. Integer
    legacy receipts and canonical decimal strings identify the same timestamp;
    floats and rounded replacement values are never repaired.
    """
    if not isinstance(handle, dict):
        return handle
    projected = dict(handle)
    if 'started_ns' in projected:
        value = projected['started_ns']
        if isinstance(value, str) and re.fullmatch(r'0|[1-9][0-9]*', value):
            value = int(value)
        if type(value) is not int or value < 0:
            raise ValueError('Job started_ns must be exact nonnegative integer nanoseconds')
        projected['started_ns'] = str(value) if value > 2**53 - 1 else value
    return projected


def same_handle(left, right):
    """Compare canonical projections against the original exact Python receipt."""
    return json.dumps(wire_handle(left), sort_keys=True, allow_nan=False) == json.dumps(wire_handle(right), sort_keys=True, allow_nan=False)


def wire_handles(value):
    """Copy outward structured results; do not touch raw artifact text or hashes."""
    if isinstance(value, dict):
        return {key: wire_handle(item) if key in ('handle', 'original_handle') else wire_handles(item)
                for key, item in value.items()}
    if isinstance(value, list):
        return [wire_handles(item) for item in value]
    return value
