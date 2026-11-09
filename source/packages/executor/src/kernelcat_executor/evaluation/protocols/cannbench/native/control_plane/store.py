"""Durable content-addressed artifacts and a transactional campaign ledger."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import secrets
import sqlite3
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

from .models import ContractError, canonical_json


@dataclass(frozen=True)
class ArtifactRef:
    sha256: str
    size: int
    media_type: str
    relative_path: str

    @property
    def uri(self) -> str:
        return f"cas://sha256/{self.sha256}"


class ContentAddressedStore:
    """Local CAS with atomic publication and read-time SHA-256 verification."""

    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()
        self.objects = self.root / "objects" / "sha256"
        self.objects.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def digest_bytes(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    def _path(self, digest: str) -> Path:
        if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise ContractError("invalid CAS digest")
        return self.objects / digest[:2] / digest[2:]

    def put_bytes(self, data: bytes, *, media_type: str = "application/octet-stream") -> ArtifactRef:
        digest = self.digest_bytes(data)
        destination = self._path(digest)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            existing = destination.read_bytes()
            if self.digest_bytes(existing) != digest or existing != data:
                raise IOError(f"CAS collision or corruption at {destination}")
        else:
            fd, temp_name = tempfile.mkstemp(prefix=".incoming-", dir=destination.parent)
            temp = Path(temp_name)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temp, destination)
            finally:
                with contextlib.suppress(FileNotFoundError):
                    temp.unlink()
        return ArtifactRef(
            sha256=digest,
            size=len(data),
            media_type=media_type,
            relative_path=destination.relative_to(self.root).as_posix(),
        )

    def put_text(self, text: str, *, media_type: str = "text/plain; charset=utf-8") -> ArtifactRef:
        return self.put_bytes(text.encode("utf-8"), media_type=media_type)

    def put_json(self, value: Any) -> ArtifactRef:
        return self.put_text(canonical_json(value) + "\n", media_type="application/json")

    def put_file(self, path: str | Path, *, media_type: str = "application/octet-stream") -> ArtifactRef:
        source = Path(path)
        if not source.is_file():
            raise FileNotFoundError(source)
        return self.put_bytes(source.read_bytes(), media_type=media_type)

    def put_tree(self, root: str | Path, *, ignore: Iterable[str] = (".git", "__pycache__")) -> ArtifactRef:
        source = Path(root).resolve()
        if not source.is_dir():
            raise NotADirectoryError(source)
        ignored = set(ignore)
        entries: list[dict[str, Any]] = []
        for path in sorted(source.rglob("*"), key=lambda item: item.as_posix()):
            relative = path.relative_to(source)
            if not path.is_file() or any(part in ignored for part in relative.parts):
                continue
            ref = self.put_file(path)
            entries.append(
                {
                    "path": relative.as_posix(),
                    "sha256": ref.sha256,
                    "size": ref.size,
                    "mode": path.stat().st_mode & 0o777,
                }
            )
        if not entries:
            raise ContractError(f"refusing to freeze an empty source tree: {source}")
        return self.put_json({"schema_version": 1, "type": "tree", "entries": entries})

    def read_bytes(self, digest_or_uri: str) -> bytes:
        digest = digest_or_uri.rsplit("/", 1)[-1]
        data = self._path(digest).read_bytes()
        if self.digest_bytes(data) != digest:
            raise IOError(f"CAS verification failed for {digest}")
        return data

    def read_json(self, digest_or_uri: str) -> Any:
        return json.loads(self.read_bytes(digest_or_uri).decode("utf-8"))

    def verify(self, digest_or_uri: str) -> bool:
        try:
            self.read_bytes(digest_or_uri)
        except (OSError, ValueError):
            return False
        return True


_RUN_TRANSITIONS: dict[str, set[str]] = {
    "planned": {"leased", "cancelled"},
    "leased": {"running", "cancelled", "failed"},
    "running": {"succeeded", "failed", "timed_out", "cancelled", "pending_remote"},
    "pending_remote": {"running", "succeeded", "failed", "timed_out", "cancelled"},
    "succeeded": set(),
    "failed": set(),
    "timed_out": set(),
    "cancelled": set(),
}


class Ledger:
    """SQLite event ledger with idempotent requests and expiring leases."""

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def _initialize(self) -> None:
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS entities (
                    entity_id TEXT PRIMARY KEY,
                    entity_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY,
                    aggregate_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    UNIQUE(aggregate_id, kind, payload_sha256)
                );
                CREATE INDEX IF NOT EXISTS events_aggregate_idx
                    ON events(aggregate_id, created_at, event_id);
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    experiment_id TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    status TEXT NOT NULL,
                    owner TEXT,
                    request_json TEXT NOT NULL,
                    request_sha256 TEXT NOT NULL,
                    result_json TEXT,
                    heartbeat_at REAL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS runs_experiment_idx
                    ON runs(experiment_id, phase, created_at);
                CREATE TABLE IF NOT EXISTS leases (
                    resource_key TEXT PRIMARY KEY,
                    owner TEXT NOT NULL,
                    token TEXT NOT NULL UNIQUE,
                    expires_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS artifacts (
                    sha256 TEXT PRIMARY KEY,
                    size INTEGER NOT NULL,
                    media_type TEXT NOT NULL,
                    relative_path TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                """
            )

    @contextlib.contextmanager
    def immediate(self) -> Iterator[sqlite3.Connection]:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
            except BaseException:
                db.execute("ROLLBACK")
                raise
            else:
                db.execute("COMMIT")

    def record_artifact(self, ref: ArtifactRef) -> None:
        with self.immediate() as db:
            row = db.execute("SELECT * FROM artifacts WHERE sha256=?", (ref.sha256,)).fetchone()
            if row:
                current = (row["size"], row["media_type"], row["relative_path"])
                incoming = (ref.size, ref.media_type, ref.relative_path)
                if current != incoming:
                    raise ContractError(f"artifact metadata collision for {ref.sha256}")
                return
            db.execute(
                "INSERT INTO artifacts VALUES(?,?,?,?,?)",
                (ref.sha256, ref.size, ref.media_type, ref.relative_path, time.time()),
            )

    def record_entity(self, entity_id: str, entity_type: str, payload: Any) -> bool:
        text = canonical_json(payload)
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        with self.immediate() as db:
            row = db.execute("SELECT payload_sha256,payload_json FROM entities WHERE entity_id=?", (entity_id,)).fetchone()
            if row:
                if row["payload_sha256"] != digest or row["payload_json"] != text:
                    raise ContractError(f"identity collision or attempted mutation: {entity_id}")
                return False
            db.execute(
                "INSERT INTO entities VALUES(?,?,?,?,?)",
                (entity_id, entity_type, text, digest, time.time()),
            )
        return True

    def get_entity(self, entity_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM entities WHERE entity_id=?", (entity_id,)).fetchone()
        if row is None:
            return None
        return {
            "entity_id": row["entity_id"],
            "entity_type": row["entity_type"],
            "payload": json.loads(row["payload_json"]),
        }

    def append_event(self, aggregate_id: str, kind: str, payload: Any) -> tuple[str, bool]:
        text = canonical_json(payload)
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        event_material = "\x1f".join((aggregate_id, kind, digest)).encode("utf-8")
        event_id = "event_" + hashlib.sha256(event_material).hexdigest()
        with self.immediate() as db:
            row = db.execute("SELECT payload_sha256,payload_json FROM events WHERE event_id=?", (event_id,)).fetchone()
            if row:
                if row["payload_sha256"] != digest or row["payload_json"] != text:
                    raise ContractError(f"event ID collision: {event_id}")
                return event_id, False
            db.execute(
                "INSERT INTO events VALUES(?,?,?,?,?,?)",
                (event_id, aggregate_id, kind, text, digest, time.time()),
            )
        return event_id, True

    def list_events(self, aggregate_id: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM events WHERE aggregate_id=? ORDER BY created_at,event_id",
                (aggregate_id,),
            ).fetchall()
        return [
            {
                "event_id": row["event_id"],
                "aggregate_id": row["aggregate_id"],
                "kind": row["kind"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def create_run(
        self,
        *,
        run_id: str,
        idempotency_key: str,
        experiment_id: str,
        phase: str,
        request: Mapping[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        text = canonical_json(request)
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        now = time.time()
        with self.immediate() as db:
            row = db.execute("SELECT * FROM runs WHERE idempotency_key=?", (idempotency_key,)).fetchone()
            if row:
                if row["request_sha256"] != digest:
                    raise ContractError("idempotency key reused with a different request")
                return self._run_row(row), False
            db.execute(
                "INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    idempotency_key,
                    experiment_id,
                    phase,
                    "planned",
                    None,
                    text,
                    digest,
                    None,
                    None,
                    now,
                    now,
                ),
            )
            row = db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return self._run_row(row), True

    @staticmethod
    def _run_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "run_id": row["run_id"],
            "idempotency_key": row["idempotency_key"],
            "experiment_id": row["experiment_id"],
            "phase": row["phase"],
            "status": row["status"],
            "owner": row["owner"],
            "request": json.loads(row["request_json"]),
            "result": json.loads(row["result_json"]) if row["result_json"] else None,
            "heartbeat_at": row["heartbeat_at"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return self._run_row(row) if row else None

    def transition_run(
        self,
        run_id: str,
        to_status: str,
        *,
        owner: str | None = None,
        result: Mapping[str, Any] | None = None,
        expected_status: str | None = None,
    ) -> dict[str, Any]:
        if to_status not in _RUN_TRANSITIONS:
            raise ContractError(f"unknown run status: {to_status}")
        with self.immediate() as db:
            row = db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(run_id)
            current = row["status"]
            if expected_status is not None and current != expected_status:
                raise ContractError(f"run {run_id} is {current}, expected {expected_status}")
            if to_status not in _RUN_TRANSITIONS[current]:
                raise ContractError(f"illegal run transition {current} -> {to_status}")
            now = time.time()
            result_json = canonical_json(result) if result is not None else row["result_json"]
            db.execute(
                "UPDATE runs SET status=?,owner=COALESCE(?,owner),result_json=?,heartbeat_at=?,updated_at=? WHERE run_id=?",
                (to_status, owner, result_json, now, now, run_id),
            )
            updated = db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return self._run_row(updated)

    def heartbeat_run(self, run_id: str, owner: str) -> None:
        with self.immediate() as db:
            row = db.execute("SELECT owner,status FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(run_id)
            if row["owner"] not in {None, owner}:
                raise ContractError(f"run {run_id} is owned by {row['owner']}")
            if row["status"] not in {"leased", "running", "pending_remote"}:
                raise ContractError(f"cannot heartbeat run in state {row['status']}")
            now = time.time()
            db.execute("UPDATE runs SET owner=?,heartbeat_at=?,updated_at=? WHERE run_id=?", (owner, now, now, run_id))

    def acquire_lease(self, resource_key: str, owner: str, *, ttl_seconds: float = 600.0) -> str:
        if ttl_seconds <= 0:
            raise ContractError("lease TTL must be positive")
        now = time.time()
        token = secrets.token_urlsafe(24)
        with self.immediate() as db:
            row = db.execute("SELECT * FROM leases WHERE resource_key=?", (resource_key,)).fetchone()
            if row and row["expires_at"] > now:
                raise ContractError(f"resource already leased by {row['owner']}: {resource_key}")
            db.execute(
                "INSERT INTO leases(resource_key,owner,token,expires_at,updated_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(resource_key) DO UPDATE SET owner=excluded.owner,token=excluded.token,"
                "expires_at=excluded.expires_at,updated_at=excluded.updated_at",
                (resource_key, owner, token, now + ttl_seconds, now),
            )
        return token

    def renew_lease(self, resource_key: str, owner: str, token: str, *, ttl_seconds: float = 600.0) -> None:
        if ttl_seconds <= 0:
            raise ContractError("lease TTL must be positive")
        now = time.time()
        with self.immediate() as db:
            row = db.execute("SELECT * FROM leases WHERE resource_key=?", (resource_key,)).fetchone()
            if row is None or row["owner"] != owner or row["token"] != token:
                raise ContractError("lease ownership mismatch")
            if row["expires_at"] <= now:
                raise ContractError("lease already expired")
            db.execute(
                "UPDATE leases SET expires_at=?,updated_at=? WHERE resource_key=?",
                (now + ttl_seconds, now, resource_key),
            )

    def release_lease(self, resource_key: str, owner: str, token: str) -> None:
        with self.immediate() as db:
            row = db.execute("SELECT * FROM leases WHERE resource_key=?", (resource_key,)).fetchone()
            if row is None:
                return
            if row["owner"] != owner or row["token"] != token:
                raise ContractError("lease ownership mismatch")
            db.execute("DELETE FROM leases WHERE resource_key=?", (resource_key,))

    def pending_runs(self, experiment_id: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM runs WHERE status NOT IN (?,?,?,?)"
        params: list[Any] = ["succeeded", "failed", "timed_out", "cancelled"]
        if experiment_id is not None:
            query += " AND experiment_id=?"
            params.append(experiment_id)
        query += " ORDER BY created_at"
        with self.connect() as db:
            rows = db.execute(query, params).fetchall()
        return [self._run_row(row) for row in rows]

    def health(self) -> dict[str, Any]:
        with self.connect() as db:
            integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
            counts = {
                table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("entities", "events", "runs", "leases", "artifacts")
            }
        return {"ok": integrity == "ok", "integrity": integrity, "counts": counts, "path": str(self.path)}
