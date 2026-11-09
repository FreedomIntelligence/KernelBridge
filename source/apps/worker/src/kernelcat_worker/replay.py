"""Finite validation of Pi-owned immutable session files at original turn boundaries."""
from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

from kernelcat_executor.capabilities.runtime_artifacts import RuntimeArtifacts
from kernelcat_executor.contracts.harness import validate_replay
from kernelcat_executor.contracts.models import Conflict, InDoubt, Json, digest
from kernelcat_executor.contracts.ports import Store

from kernelcat_worker.rsi_records import Records

MAX_SESSION_BYTES = 32 * 1024 * 1024


class NativeReplayFiles:
    def __init__(self, store: Store, state: Path, workspace: Path, task_id: str,
                 *, plugin: Json | None = None) -> None:
        self.store, self.workspace, self.task_id = store, workspace.resolve(), task_id
        self.plugin = plugin
        self.records = Records(store, RuntimeArtifacts(state / "runtime-artifacts"), task_id)
        self.branch = self.records.latest("branch")
        self.directory = state.resolve() / "pi-sessions" / digest([task_id, str(self.workspace)])
        if self.directory.is_relative_to(self.workspace) or self.workspace.is_relative_to(self.directory):
            raise Conflict("Pi session storage must be disjoint from the model workspace")

    def _boundary(self) -> None:
        records = [item for item in self.store.snapshot()["actions"].values()
                   if item["action"]["task"] == self.task_id]
        ended = {item["outcome"]["data"]["proposal_id"] for item in records
                 if item["action"]["tool"] == "__turn_end__" and item.get("terminal")}
        for item in records:
            if not item.get("terminal") or item["outcome"]["status"] == "unknown":
                raise InDoubt("native restore requires reconciliation: " + item["action"]["id"])
            if (item["action"]["tool"] == "__propose__" and item["outcome"]["status"] == "completed"
                    and item["action"]["id"] not in ended):
                raise InDoubt("native restore has an uncheckpointed model turn: " + item["action"]["id"])

    def start(self, latest: Json | None) -> Json:
        self._boundary()
        branch = self.records.latest("branch")
        if branch != self.branch or branch is not None and branch["value"]["native"]["workspace"] != str(self.workspace):
            raise Conflict("native session workspace was frozen by a newer branch")
        for name in ("agent", "sessions", "checkpoints", "history"):
            path = self.directory / name
            path.mkdir(parents=True, exist_ok=True)
            if path.resolve() != path:
                raise Conflict("Pi replay storage cannot use symlinked roots")
        checkpoint = None
        if branch is not None:
            boundary = branch["value"]["start_step"]
            if latest is None or latest["step"] + 1 < boundary:
                raise Conflict("branch checkpoint is ahead of the original global task step")
            if latest["step"] + 1 == boundary:
                latest = {**latest, "native_checkpoint": branch["value"]["native"]["checkpoint"]}
        if latest is not None:
            if "native_checkpoint" not in latest:
                raise Conflict("historical turn has no qualified native Pi checkpoint")
            checkpoint = self.checkpoint(latest["native_checkpoint"])
        capability: Json = {"schema_version": "kernelcat.pi-replay/v1",
            "directory": str(self.directory), "workspace": str(self.workspace),
            "checkpoint": checkpoint}
        if self.plugin is not None:
            capability["plugin"] = self.plugin
        return validate_replay(capability, "NativeReplay")

    def checkpoint(self, value: object) -> Json:
        checked = validate_replay(value, "NativeCheckpoint")
        path = self.directory / "checkpoints" / (checked["digest"][7:] + ".jsonl")
        if path.parent.resolve() != path.parent:
            raise Conflict("Pi checkpoint root changed")
        with open(path, "rb", opener=lambda name, flags: os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK)) as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_SESSION_BYTES:
                raise Conflict("Pi checkpoint must be a bounded regular file")
            contents = stream.read(MAX_SESSION_BYTES + 1)
        if len(contents) > MAX_SESSION_BYTES or "sha256:" + hashlib.sha256(contents).hexdigest() != checked["digest"]:
            raise Conflict("Pi checkpoint digest differs from its immutable bytes")
        try:
            header = json.loads(contents.split(b"\n", 1)[0])
            leaf = json.loads(contents.rstrip().rsplit(b"\n", 1)[-1])
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise Conflict("Pi checkpoint header is not valid UTF-8 JSON") from error
        if (not isinstance(header, dict) or header.get("type") != "session" or header.get("version") != 3
                or header.get("id") != checked["session_id"] or header.get("cwd") != str(self.workspace)):
            raise Conflict("Pi checkpoint session identity/workspace differs from its owner")
        # Pi v3 rebuilds its active leaf from the last non-header entry. Node
        # validates the full tree through the pinned SDK before submitting bytes.
        if not isinstance(leaf, dict) or leaf.get("type") == "session" or leaf.get("id") != checked["leaf_id"]:
            raise Conflict("Pi checkpoint leaf differs from its retained file boundary")
        return checked
