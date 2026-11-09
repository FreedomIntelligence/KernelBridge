"""Fixed standard-library remote wrapper, staged by RemoteJobs after admission."""
from __future__ import annotations

import base64
import binascii
import fcntl
import hashlib
import json
import math
import os
import stat
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

MAX_REQUEST_BYTES = 8 * 1024 * 1024
MAX_METADATA_BYTES = 4 * 1024 * 1024
MAX_RESULT_BYTES = 8 * 1024 * 1024
MAX_RESPONSE_BYTES = MAX_RESULT_BYTES + 16384
MAX_SUBMISSION_BYTES = 256 * 1024 * 1024
SCHEMA = "kernelcat.remote-job/v1"


class StateError(ValueError):
    def __init__(self, kind: str, reason: str) -> None:
        self.kind = kind
        super().__init__(reason)


def encode_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode()


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("JSON repeats an object key")
        value[key] = item
    return value


def _invalid_constant(value: str) -> None:
    raise ValueError("JSON contains a non-finite number: " + value)


def decode_json(data: bytes) -> Any:
    value = json.loads(data, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
    _json_value(value)
    return value


def _json_value(value: Any) -> None:
    if value is None or type(value) in {str, bool, int}:
        return
    if type(value) is float and math.isfinite(value):
        return
    if type(value) is list:
        for item in value:
            _json_value(item)
        return
    if type(value) is dict and all(type(key) is str for key in value):
        for item in value.values():
            _json_value(item)
        return
    raise ValueError("request/result contains a value outside JSON")


def absolute_path(value: str) -> str:
    if not isinstance(value, str) or "\0" in value:
        raise ValueError("remote root and Python require absolute canonical paths")
    path = PurePosixPath(value)
    if not path.is_absolute() or str(path) != value or ".." in path.parts or value == "/":
        raise ValueError("remote root and Python require absolute canonical paths")
    return value


def _source_path(value: str) -> str:
    if not isinstance(value, str) or not value or "\0" in value or "\\" in value:
        raise ValueError("staged files require safe relative paths")
    path = PurePosixPath(value)
    if (path.is_absolute() or str(path) != value or not path.parts
            or ".." in path.parts or path.parts[0] == ".kernelcat"):
        raise ValueError("staged files require safe relative paths outside .kernelcat")
    return value


def prepare_submission(identity: str, request: Any, files: Mapping[str, bytes],
                       argv: Sequence[str], python: str, runner: bytes) -> dict[str, Any]:
    if not isinstance(identity, str) or not identity or len(identity.encode()) > 2048 or "\0" in identity:
        raise ValueError("remote job requires an original bounded identity")
    if type(request) is not dict:
        raise ValueError("remote request must be a JSON object")
    _json_value(request)
    request_bytes = encode_json(request)
    if len(request_bytes) > MAX_REQUEST_BYTES:
        raise ValueError("remote request exceeds its byte bound")
    file_bytes = _checked_files(files)
    arguments = _checked_argv(argv, file_bytes)
    manifest = {"schema_version": SCHEMA, "identity": identity,
                "request_sha256": hashlib.sha256(request_bytes).hexdigest(),
                "argv": arguments, "python": absolute_path(python),
                "files": {name: hashlib.sha256(data).hexdigest() for name, data in file_bytes.items()},
                "runner_sha256": hashlib.sha256(runner).hexdigest()}
    if len(encode_json(manifest)) > MAX_METADATA_BYTES:
        raise ValueError("remote manifest exceeds its byte bound")
    submission = {"manifest": manifest, "request": base64.b64encode(request_bytes).decode(),
                  "files": {name: base64.b64encode(data).decode() for name, data in file_bytes.items()},
                  "runner": base64.b64encode(runner).decode()}
    if len(encode_json(submission)) > MAX_SUBMISSION_BYTES:
        raise ValueError("remote submission exceeds its byte bound")
    return submission


def _checked_files(files: Mapping[str, bytes]) -> dict[str, bytes]:
    if not isinstance(files, Mapping) or not files:
        raise ValueError("remote job requires staged program bytes")
    result: dict[str, bytes] = {}
    directories: set[str] = set()
    for name, data in files.items():
        name = _source_path(name)
        if not isinstance(data, bytes):
            raise ValueError("staged files must contain bytes")
        if name in directories or name in result:
            raise ValueError("staged file paths overlap")
        for parent in PurePosixPath(name).parents:
            if str(parent) in result:
                raise ValueError("staged file paths overlap")
            directories.add(str(parent))
        result[name] = data
    return result


def _checked_argv(argv: Sequence[str], files: Mapping[str, object]) -> list[str]:
    if isinstance(argv, (str, bytes)) or not isinstance(argv, Sequence) or not argv:
        raise ValueError("remote argv requires a staged Python program")
    arguments = list(argv)
    if (any(not isinstance(value, str) or "\0" in value for value in arguments)
            or arguments[0] not in files or not arguments[0].endswith(".py")
            or arguments[0].startswith("-")):
        raise ValueError("remote argv must begin with a staged relative Python program")
    return arguments


def _directory(path: Path) -> None:
    try:
        resolved = path.resolve()
    except RuntimeError as error:
        raise StateError("invalid", "remote directory contains a symlink cycle") from error
    if resolved != path:
        raise StateError("invalid", "remote directory contains a symlink")
    if path.exists() and not path.is_dir():
        raise StateError("invalid", "remote path is not a directory")


def _read(path: Path, limit: int) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise StateError("invalid", "remote metadata is not a regular file")
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise StateError("invalid", "remote metadata/result exceeds its byte bound")
    return data


def _write(path: Path, data: bytes, *, replace: bool = False) -> None:
    temporary = path.with_name("." + path.name + ".tmp") if replace else path
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o400)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    if replace:
        if path.is_symlink():
            raise StateError("invalid", "remote metadata is a symlink")
        os.replace(temporary, path)
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _json_file(path: Path) -> dict[str, Any]:
    value = decode_json(_read(path, MAX_METADATA_BYTES))
    if not isinstance(value, dict):
        raise StateError("invalid", "remote metadata must be a JSON object")
    return value


def _digest(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _manifest(metadata: Path) -> dict[str, Any]:
    value = _json_file(metadata / "manifest.json")
    required = {"schema_version", "identity", "request_sha256", "argv", "python", "files", "runner_sha256"}
    if (set(value) != required or value["schema_version"] != SCHEMA
            or not isinstance(value["identity"], str) or not value["identity"]
            or not _digest(value["request_sha256"]) or not _digest(value["runner_sha256"])
            or not isinstance(value["files"], dict) or not value["files"]
            or not all(_digest(digest) for digest in value["files"].values())):
        raise StateError("invalid", "malformed remote immutable manifest")
    _checked_files({name: b"" for name in value["files"]})
    _checked_argv(value["argv"], value["files"])
    absolute_path(value["python"])
    return value


def _handle(directory: Path, identity: str, request_sha256: str) -> dict[str, str]:
    return {"identity": identity, "request_sha256": request_sha256, "directory": str(directory)}


def _validated_submission(data: bytes) -> dict[str, Any]:
    value = decode_json(data)
    if not isinstance(value, dict) or set(value) != {"manifest", "request", "files", "runner"}:
        raise StateError("invalid", "malformed remote submission")
    manifest = value["manifest"]
    if not isinstance(manifest, dict) or not isinstance(value["files"], dict):
        raise StateError("invalid", "remote manifest/files must be JSON objects")
    request = decode_json(base64.b64decode(value["request"], validate=True))
    files = {name: base64.b64decode(content, validate=True) for name, content in value["files"].items()}
    runner = base64.b64decode(value["runner"], validate=True)
    expected = prepare_submission(manifest["identity"], request, files, manifest["argv"],
                                  manifest["python"], runner)
    if value != expected:
        raise StateError("invalid", "remote submission does not match its canonical input binding")
    return value


def _matches(manifest: dict[str, Any], handle: dict[str, str]) -> None:
    if (manifest.get("schema_version") != SCHEMA or manifest.get("identity") != handle["identity"]
            or manifest.get("request_sha256") != handle["request_sha256"]):
        raise StateError("conflict", "original identity/request binding differs")


def _submit(directory: Path, handle: dict[str, str], data: bytes) -> dict[str, Any]:
    submission = _validated_submission(data)
    manifest = submission["manifest"]
    _matches(manifest, handle)
    directory.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _directory(directory.parent)
    try:
        directory.mkdir(mode=0o700)
    except FileExistsError:
        return _existing(directory, handle, manifest)
    try:
        _stage_and_launch(directory, handle, submission)
    except (OSError, ValueError) as error:
        raise StateError("uncertain", "original directory retained after staging/launch failure: "
                         + str(error)) from error
    return {"ok": True, "handle": handle}


def _stage_and_launch(directory: Path, handle: dict[str, str], submission: dict[str, Any]) -> None:
    manifest = submission["manifest"]
    metadata = directory / ".kernelcat"
    metadata.mkdir(mode=0o700)
    _write(metadata / "request.json", base64.b64decode(submission["request"]))
    for name, content in submission["files"].items():
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _write(path, base64.b64decode(content))
    _write(metadata / "runner.py", base64.b64decode(submission["runner"]))
    _write(metadata / "process.lock", b"")
    _write(metadata / "manifest.json", encode_json(manifest), replace=True)
    _write(metadata / "launch.json", encode_json({**handle, "state": "admitted"}), replace=True)
    with (metadata / "runner.stderr.log").open("xb") as errors:
        subprocess.Popen([manifest["python"], "-I", "-B", str(metadata / "runner.py"),
                          "execute", str(directory)], cwd=directory, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=errors, start_new_session=True,
                         close_fds=True)


def _existing(directory: Path, handle: dict[str, str], expected: dict[str, Any]) -> dict[str, Any]:
    _directory(directory)
    metadata = directory / ".kernelcat"
    _directory(metadata)
    try:
        retained = _manifest(metadata)
    except FileNotFoundError as error:
        raise StateError("uncertain", "existing incomplete job; never launch it again") from error
    if retained != expected:
        raise StateError("conflict", "identity already binds different request, argv, files or runner")
    return {"ok": True, "handle": handle}


def _verify_inputs(directory: Path, manifest: dict[str, Any]) -> bytes:
    metadata = directory / ".kernelcat"
    if _manifest(metadata) != manifest:
        raise StateError("invalid", "remote immutable manifest changed")
    request = _read(metadata / "request.json", MAX_REQUEST_BYTES)
    if hashlib.sha256(request).hexdigest() != manifest["request_sha256"]:
        raise StateError("invalid", "remote immutable request changed")
    expected = {**manifest["files"], ".kernelcat/runner.py": manifest["runner_sha256"]}
    remaining_bytes = MAX_SUBMISSION_BYTES - len(request)
    for name, digest in expected.items():
        path = directory / name
        _directory(path.parent)
        actual, size = _input_digest(path, remaining_bytes)
        remaining_bytes -= size
        if actual != digest:
            raise StateError("invalid", "remote immutable file changed: " + name)
    return request


def _input_digest(path: Path, byte_limit: int) -> tuple[str, int]:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        file_state = os.fstat(stream.fileno())
        if not stat.S_ISREG(file_state.st_mode):
            raise StateError("invalid", "staged input is not a regular file")
        if file_state.st_size > byte_limit:
            raise StateError("invalid", "remote immutable inputs exceed their byte bound")
        digest = hashlib.sha256()
        size = 0
        # Bound concurrent file growth as well as an already enlarged retained file.
        while chunk := stream.read(min(1024 * 1024, byte_limit - size + 1)):
            size += len(chunk)
            if size > byte_limit:
                raise StateError("invalid", "remote immutable inputs exceed their byte bound")
            digest.update(chunk)
    return digest.hexdigest(), size


def _execute(directory: Path) -> None:
    metadata = directory / ".kernelcat"
    _directory(directory)
    _directory(metadata)
    manifest = _manifest(metadata)
    with _lock_file(metadata) as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        start = {"identity": manifest["identity"], "request_sha256": manifest["request_sha256"],
                 "pid": os.getpid(), "start_identity": str(time.time_ns())}
        _write(metadata / "started.json", encode_json(start))
        state = {**start, "schema_version": SCHEMA, "status": "running", "exit_status": None,
                 "result_sha256": None, "reason": None}
        _write(metadata / "status.json", encode_json(state), replace=True)
        _run_program(directory, manifest, state)


def _run_program(directory: Path, manifest: dict[str, Any], state: dict[str, Any]) -> None:
    metadata = directory / ".kernelcat"
    try:
        request = _verify_inputs(directory, manifest)
        with (metadata / "stdout.log").open("xb") as output, (metadata / "stderr.log").open("xb") as errors:
            completed = subprocess.run([manifest["python"], *manifest["argv"]], cwd=directory,
                                       input=request, stdout=output, stderr=errors, check=False)
        state["exit_status"] = completed.returncode
        _verify_inputs(directory, manifest)
        if completed.returncode != 0:
            raise ValueError("program exited with status " + str(completed.returncode))
        result = decode_json(_read(metadata / "stdout.log", MAX_RESULT_BYTES))
        if not isinstance(result, dict):
            raise ValueError("program stdout must be one JSON object")
        result_bytes = encode_json(result)
        _write(metadata / "result.json", result_bytes, replace=True)
        state.update(status="completed", result_sha256=hashlib.sha256(result_bytes).hexdigest())
    except (OSError, ValueError) as error:
        state.update(status="failed", reason=(type(error).__name__ + ": " + str(error))[:2048])
    _write(metadata / "status.json", encode_json(state), replace=True)


def _lock_file(metadata: Path) -> BinaryIO:
    descriptor = os.open(metadata / "process.lock", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise StateError("invalid", "remote process lock is not a regular file")
    return os.fdopen(descriptor, "rb")


def _active(metadata: Path) -> bool:
    with _lock_file(metadata) as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
    return False


def _status(directory: Path, handle: dict[str, str]) -> dict[str, Any]:
    response: dict[str, Any] = {"ok": True, "handle": handle, "status": "unknown", "result": None,
                                "reason": "job has no retained launch/result; never redispatch"}
    _directory(directory)
    metadata = directory / ".kernelcat"
    _directory(metadata)
    if not directory.exists():
        return response
    try:
        manifest = _manifest(metadata)
        _matches(manifest, handle)
        state = _json_file(metadata / "status.json")
    except FileNotFoundError:
        return response
    _validate_state(state, handle, metadata)
    if state["status"] == "running":
        if _active(metadata):
            response.update(status="running", reason=None)
        else:
            response["reason"] = "original runner is no longer active without a terminal record"
        return response
    if state["status"] == "completed":
        _verify_inputs(directory, manifest)
        result_bytes = _read(metadata / "result.json", MAX_RESULT_BYTES)
        if hashlib.sha256(result_bytes).hexdigest() != state["result_sha256"]:
            raise StateError("invalid", "remote result digest changed")
        result = decode_json(result_bytes)
        if not isinstance(result, dict):
            raise StateError("invalid", "completed remote result is not a JSON object")
        response.update(status="completed", result=result, reason=None)
    else:
        response.update(status="failed", reason=state["reason"])
    return response


def _validate_state(state: dict[str, Any], handle: dict[str, str], metadata: Path) -> None:
    required = {"schema_version", "identity", "request_sha256", "pid", "start_identity", "status",
                "exit_status", "result_sha256", "reason"}
    if set(state) != required:
        raise StateError("invalid", "malformed remote status fields")
    _matches(state, handle)
    start = _json_file(metadata / "started.json")
    if (type(state["pid"]) is not int or state["pid"] <= 0
            or not isinstance(state["start_identity"], str) or not state["start_identity"].isdigit()
            or start != {key: state[key] for key in ("identity", "request_sha256", "pid", "start_identity")}):
        raise StateError("invalid", "remote status does not bind its original process start")
    status, exit_status, reason = state["status"], state["exit_status"], state["reason"]
    if status == "running" and exit_status is None and state["result_sha256"] is None and reason is None:
        return
    if status == "completed" and type(exit_status) is int and exit_status == 0 and reason is None:
        digest = state["result_sha256"]
        if _digest(digest):
            return
    if (status == "failed" and (exit_status is None or type(exit_status) is int)
            and state["result_sha256"] is None and isinstance(reason, str) and 0 < len(reason) <= 2048):
        return
    raise StateError("invalid", "remote status has inconsistent completion/exit/result fields")


def main() -> None:
    if len(sys.argv) == 3 and sys.argv[1] == "execute":
        _execute(Path(absolute_path(sys.argv[2])))
        return
    try:
        if len(sys.argv) != 6 or sys.argv[1] not in {"submit", "status"}:
            raise StateError("invalid", "invalid finite remote runner operation")
        operation, root, directory_name, identity, request_sha256 = sys.argv[1:]
        root = absolute_path(root)
        directory = Path(absolute_path(directory_name))
        if directory_name != root + "/" + hashlib.sha256(identity.encode()).hexdigest():
            raise StateError("invalid", "remote directory differs from original identity")
        _directory(Path(root))
        handle = _handle(directory, identity, request_sha256)
        if operation == "submit":
            data = sys.stdin.buffer.read(MAX_SUBMISSION_BYTES + 1)
            if len(data) > MAX_SUBMISSION_BYTES:
                raise StateError("invalid", "remote submission exceeds its byte bound")
            response = _submit(directory, handle, data)
        else:
            response = _status(directory, handle)
    except (OSError, ValueError, KeyError, TypeError, binascii.Error) as error:
        response = {"ok": False, "kind": error.kind if isinstance(error, StateError) else "invalid",
                    "reason": (type(error).__name__ + ": " + str(error))[:2048]}
    sys.stdout.buffer.write(encode_json(response) + b"\n")


if __name__ == "__main__":
    main()
