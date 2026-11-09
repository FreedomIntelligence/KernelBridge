"""The executable worker selects concrete capabilities and owns their lifetime."""
from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import BinaryIO

from kernelcat_executor.contracts.context import validate_context_value
from kernelcat_executor.contracts.models import Json, Task

from kernelcat_worker.bindings import build
from kernelcat_worker.configuration import Config, PiWorkerConfig, load_config
from kernelcat_worker.startup import admit_config
from kernelcat_worker.transport import serve_stream


def _input_file(path: Path, label: str) -> Path:
    if not path.is_absolute() or not path.is_file():
        raise ValueError(f"{label} must be an existing absolute file path")
    return path.resolve()


def _active_harness(config: Config, base: Path, environment: Mapping[str, str]) -> None:
    root = environment.get("KERNELCAT_ACTIVE_AGENT_ROOT")
    node = environment.get("KERNELCAT_ACTIVE_NODE")
    if not root or not node:
        raise ValueError("Node-owned worker requires KERNELCAT_ACTIVE_AGENT_ROOT and KERNELCAT_ACTIVE_NODE")
    if not Path(root).is_absolute() or not Path(node).is_absolute():
        raise ValueError("active Agent root and Node executable must be absolute paths")
    if (base / config.pi_worker.root).resolve(strict=True) != Path(root).resolve(strict=True):
        raise ValueError("configured Pi worker root differs from the active Node Agent root")
    configured_node = Path(config.pi_worker.node)
    if not configured_node.is_absolute() or configured_node.resolve(strict=True) != Path(node).resolve(strict=True):
        raise ValueError("configured Pi Node executable differs from the active Node executable")


def _active_transport(config: Config, environment: Mapping[str, str]) -> Config:
    raw = environment.get("KERNELCAT_MAX_FRAME_BYTES")
    if raw is None:
        raise ValueError("Node-owned worker requires KERNELCAT_MAX_FRAME_BYTES from its active launcher")
    try:
        worker = PiWorkerConfig.model_validate({**config.pi_worker.model_dump(mode="python"),
                                               "max_frame_bytes": int(raw)})
    except ValueError as error:
        raise ValueError("active KERNELCAT_MAX_FRAME_BYTES must be an integer accepted by PiWorkerConfig") from error
    return config.model_copy(update={"pi_worker": worker})


def serve(config_path: Path, task_path: Path, reader: BinaryIO, writer: BinaryIO, *,
          expected_modules: object | None = None) -> Json:
    config_path = _input_file(config_path, "worker configuration")
    task_path = _input_file(task_path, "worker task")
    config = load_config(config_path)
    startup = admit_config(config, expected_modules=expected_modules)
    config = startup.bind_config(config)
    _active_harness(config, config_path.parent, os.environ)
    config = _active_transport(config, os.environ)
    validate_context_value(config.model_dump(mode="python"), "worker configuration")
    task = Task.model_validate_json(task_path.read_text())
    validate_context_value(task.model_dump(mode="python"), "worker task")
    host = build(config, config_path.parent, task, probe_versions=False,
                 external_context=True, startup=startup)
    try:
        return host.serve(task, lambda handle: serve_stream(
            reader, writer, handle, max_frame_bytes=config.pi_worker.max_frame_bytes))
    finally:
        host.close()


def serve_runtime(config_path: Path, manifest_path: Path | None,
                  reader: BinaryIO, writer: BinaryIO, *,
                  expected_modules: object | None = None) -> Json:
    """Verify the active launcher and exact launch bytes before opening any state."""
    from kernelcat_executor.capabilities.worker import decode_object
    from kernelcat_executor.contracts.runtime import RUNTIME_BYTES, bytes_digest, validate_launch

    from kernelcat_worker.runtime import RuntimeService

    config_path = _input_file(config_path, "worker configuration")
    config = load_config(config_path)
    startup = admit_config(config, expected_modules=expected_modules)
    config = startup.bind_config(config)
    _active_harness(config, config_path.parent, os.environ)
    config = _active_transport(config, os.environ)
    validate_context_value(config.model_dump(mode="python"), "worker configuration")
    base = config_path.parent
    workspace, state = (base / config.workspace).resolve(), (base / config.state_dir).resolve()
    protected = [config_path, state, (base / config.pi_worker.root).resolve(),
                 *(base / root for root in config.skill_roots)]
    manifest = None
    checksum = None
    if manifest_path is not None:
        manifest_path = _input_file(manifest_path, "run manifest")
        active_path = os.environ.get("KERNELCAT_ACTIVE_RUN_MANIFEST_PATH")
        if active_path is None or not Path(active_path).is_absolute() or Path(active_path).resolve() != manifest_path:
            raise ValueError("run manifest differs from KERNELCAT_ACTIVE_RUN_MANIFEST_PATH")
        with manifest_path.open("rb") as stream:
            raw = stream.read(RUNTIME_BYTES + 4097)
        if len(raw) > RUNTIME_BYTES + 4096:
            raise ValueError("run launch manifest exceeds its bounded UTF-8 bytes")
        checksum = bytes_digest(raw)
        if os.environ.get("KERNELCAT_ACTIVE_RUN_MANIFEST_SHA256") != checksum:
            raise ValueError("run manifest exact bytes differ from KERNELCAT_ACTIVE_RUN_MANIFEST_SHA256")
        manifest = dict(validate_launch(decode_object(raw)))
        protected.append(manifest_path)
    elif any(name in os.environ for name in (
            "KERNELCAT_ACTIVE_RUN_MANIFEST_PATH", "KERNELCAT_ACTIVE_RUN_MANIFEST_SHA256")):
        raise ValueError("manifest launcher variables require explicit --manifest")
    if any(path.resolve().is_relative_to(workspace) or workspace.is_relative_to(path.resolve())
           for path in protected):
        raise ValueError("config, manifest, controller, skills, state and writable workspace must be disjoint")
    service = RuntimeService(config, base, manifest=manifest, manifest_digest=checksum, startup=startup)
    try:
        return serve_stream(reader, writer, service.handle,
                            max_frame_bytes=config.pi_worker.max_frame_bytes, runtime=True)
    finally:
        service.close()
