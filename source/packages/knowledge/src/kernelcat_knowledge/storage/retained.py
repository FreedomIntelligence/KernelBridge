"""Durable Knowledge intent metadata and immutable retained payload blobs."""

from __future__ import annotations

import os
import sqlite3
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from kernelcat_knowledge.contracts import ContractError
from kernelcat_knowledge.contracts.runtime import wire_digest
from kernelcat_knowledge.evidence.source_content import bytes_digest
from kernelcat_knowledge.resources.binding import ResourceBinding
from kernelcat_knowledge.storage.catalog_io import (
    catalog_lock,
    check_directory,
    install,
    read_bytes,
)

MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_MEMBERS = 16384

_TABLES = (
    "CREATE TABLE header (root TEXT NOT NULL, authority TEXT NOT NULL, revision INTEGER NOT NULL)",
    (
        "CREATE TABLE captures (intent TEXT PRIMARY KEY, body TEXT NOT NULL, consumer TEXT UNIQUE NOT NULL, "
        "registration TEXT NOT NULL, trial_binding TEXT UNIQUE NOT NULL, selection TEXT, receipt TEXT, invalidated INTEGER NOT NULL DEFAULT 0)"
    ),
    "CREATE TABLE dependencies (body TEXT NOT NULL, capture TEXT NOT NULL, PRIMARY KEY(body, capture))",
    "CREATE TABLE views (digest TEXT PRIMARY KEY, reference TEXT NOT NULL, invalidated INTEGER NOT NULL DEFAULT 0)",
    "CREATE TABLE view_members (body TEXT NOT NULL, view TEXT NOT NULL, PRIMARY KEY(body,view))",
    "CREATE TABLE retractions (intent TEXT PRIMARY KEY, body TEXT NOT NULL, retraction TEXT NOT NULL)",
    "CREATE TABLE denials (body TEXT PRIMARY KEY)",
    (
        "CREATE TABLE fences (intent TEXT NOT NULL, consumer TEXT NOT NULL COLLATE BINARY, request TEXT NOT NULL, "
        "ack TEXT, PRIMARY KEY(intent, consumer))"
    ),
    (
        "CREATE INDEX fences_pending_consumer ON fences(consumer,intent COLLATE BINARY) "
        "WHERE ack IS NULL"
    ),
)


class RetainedStore:
    def __init__(self, root: Path, *, binding: ResourceBinding | None = None) -> None:
        self.binding = binding
        self.root = root.resolve()
        self.base = self.root / "artifacts/kcwiki/retained"
        self.database = self.base / "control/control.sqlite3"
        self.authority = wire_digest(
            {
                "schema_version": "kernelcat.knowledge-authority/v1",
                "canonical_root": str(self.root),
            }
        )

    def _check_header(self, connection: sqlite3.Connection) -> None:
        row = connection.execute("SELECT root, authority FROM header").fetchone()
        if row is None or tuple(row) != (str(self.root), self.authority):
            raise ContractError(
                "Knowledge control authority was copied or its root identity changed"
            )

    def _check_path(self) -> None:
        check_directory(self.database.parent)
        if (self.database.exists() or self.database.is_symlink()) and not stat.S_ISREG(
            self.database.lstat().st_mode
        ):
            raise ContractError("Knowledge control database must be a regular file")

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection | None]:
        self._check_path()
        if not self.database.exists():
            yield None
            return
        connection = sqlite3.connect(self.database.as_uri() + "?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("BEGIN")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if (
                version == 0
                and not connection.execute("SELECT 1 FROM sqlite_master").fetchone()
            ):
                yield None
            else:
                if version != 1:
                    raise ContractError(
                        "Knowledge control metadata has an unsupported format"
                    )
                self._check_header(connection)
                yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        # Control commits follow the physically ordered resource-owner locks.
        # It owns only metadata commits; large payload I/O and callbacks stay outside.
        with catalog_lock(self.database.parent):
            self._check_path()
            connection = sqlite3.connect(self.database, isolation_level=None)
            connection.row_factory = sqlite3.Row
            try:
                connection.execute("PRAGMA synchronous=FULL")
                connection.execute("BEGIN IMMEDIATE")
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                if (
                    version == 0
                    and not connection.execute("SELECT 1 FROM sqlite_master").fetchone()
                ):
                    for statement in _TABLES:
                        connection.execute(statement)
                    connection.execute(
                        "INSERT INTO header VALUES (?, ?, 0)",
                        (str(self.root), self.authority),
                    )
                    connection.execute("PRAGMA user_version=1")
                    # A refused first request must not roll back the authority
                    # header into an unreadable empty SQLite file.
                    connection.commit()
                    connection.execute("BEGIN IMMEDIATE")
                elif version != 1:
                    raise ContractError(
                        "Knowledge control metadata has an unsupported format"
                    )
                self._check_header(connection)
                yield connection
                connection.commit()
                descriptor = os.open(self.database.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()

    def blob_path(self, digest: str) -> Path:
        hexadecimal = digest.removeprefix("sha256:")
        if len(hexadecimal) != 64 or any(
            ch not in "0123456789abcdef" for ch in hexadecimal
        ):
            raise ContractError("invalid retained blob digest")
        return self.base / "blobs/sha256" / hexadecimal[:2] / hexadecimal

    def assert_view_active(self, digest: str, reference: str) -> None:
        with self.read() as connection:
            row = (
                None
                if connection is None
                else connection.execute(
                    "SELECT reference,invalidated FROM views WHERE digest=?", (digest,)
                ).fetchone()
            )
            if row is None or row["reference"] != reference:
                raise ContractError("retained view is not registered at this authority")
            if row["invalidated"]:
                raise ContractError(
                    "retained view has a permanently retracted dependency"
                )

    def put_blob(self, content: bytes) -> str:
        if len(content) > MAX_ARTIFACT_BYTES:
            raise ContractError("retained artifact exceeds 16 MiB")
        digest = bytes_digest(content)
        install(self.blob_path(digest), content)
        return digest

    def blob(self, digest: str, expected_bytes: int) -> bytes:
        if (
            type(expected_bytes) is not int
            or not 0 <= expected_bytes <= MAX_ARTIFACT_BYTES
        ):
            raise ContractError("invalid retained artifact length")
        raw = read_bytes(self.blob_path(digest), expected_bytes)
        if len(raw) != expected_bytes or bytes_digest(raw) != digest:
            raise ContractError(f"retained blob is corrupt: {digest}")
        return raw
