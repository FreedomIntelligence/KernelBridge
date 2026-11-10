"""Freeze the retained RSI source while preserving the agent's working draft."""
from kernelcat_executor.capabilities.runtime_artifacts import workspace_bundle
from kernelcat_executor.contracts.models import Conflict


def retained_bundle(workspace, original, records, source_paths):
    source = records.baseline()["value"]["source"]
    paths = set(source_paths)
    if len(source) != len(paths) or {item["path"] for item in source} != paths:
        raise Conflict("retained RSI source differs from the configured source paths")
    # The workspace and Pi history remain the exact draft at generation stop.
    # The Program references only the source selected by the acceptance ledger.
    return workspace_bundle(workspace, original, source_overrides=source)
