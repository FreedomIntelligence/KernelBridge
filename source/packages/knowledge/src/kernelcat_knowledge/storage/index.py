"""Rebuildable SQLite FTS5 read model.

The database is deliberately disposable: JSON cards and evidence remain the
authority.  A stale or unavailable database never changes scope or trust; the
application falls back to deterministic card projection.
"""
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kernelcat_knowledge.contracts import canonical_json


def _items(value: Any) -> list[Any]:
    if value in (None, ""):
        return []
    return list(value) if isinstance(value, (list, tuple, set)) else [value]


def _flat(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, Mapping):
        return [text for key, item in value.items() for text in [str(key), *_flat(item)]]
    if isinstance(value, (list, tuple, set)):
        return [text for item in value for text in _flat(item)]
    return [str(value)]


def _scope_value(card: Mapping[str, Any], *keys: str) -> str:
    applicability = card.get("applicability")
    applicability = applicability if isinstance(applicability, Mapping) else {}
    task = card.get("task")
    task = task if isinstance(task, Mapping) else {}
    values: list[str] = []
    for key in keys:
        values.extend(str(item) for item in _items(applicability.get(key)) if item not in (None, ""))
        values.extend(str(item) for item in _items(task.get(key)) if item not in (None, ""))
        values.extend(str(item) for item in _items(card.get(key)) if item not in (None, ""))
    return " ".join(dict.fromkeys(values))


def _search_text(card: Mapping[str, Any]) -> str:
    fields = (
        card.get("card_id"), card.get("ID"), card.get("title"), card.get("summary"),
        card.get("decision_question"), card.get("tags"), card.get("mechanism_family"),
        card.get("policy_id"), card.get("claims"), card.get("decision"),
        card.get("use_when"), card.get("reject_when"), card.get("applicability"),
        card.get("revisit_when"), card.get("next_evidence_action"),
    )
    return "\n".join(text for value in fields for text in _flat(value) if text)


class SQLiteReadModel:
    schema_version = "kernelcat.sqlite-read-model/v1"

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    @staticmethod
    def _connect(path: Path) -> sqlite3.Connection:
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _create_schema(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            PRAGMA journal_mode=DELETE;
            PRAGMA synchronous=FULL;
            CREATE TABLE metadata (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL
            );
            CREATE TABLE cards (
              card_id TEXT PRIMARY KEY,
              revision_id TEXT NOT NULL,
              card_type TEXT NOT NULL,
              status TEXT NOT NULL,
              evidence_maturity TEXT NOT NULL,
              operator TEXT NOT NULL,
              hardware TEXT NOT NULL,
              toolchain TEXT NOT NULL,
              dtype TEXT NOT NULL,
              layout TEXT NOT NULL,
              parent_family TEXT NOT NULL,
              body_json TEXT NOT NULL
            );
            CREATE VIRTUAL TABLE cards_fts USING fts5(
              card_id UNINDEXED,
              title,
              summary,
              body,
              tags,
              tokenize='unicode61 remove_diacritics 2'
            );
            """
        )

    def rebuild(
        self,
        cards: Sequence[Mapping[str, Any]],
        *,
        active_index_revision: str,
    ) -> dict[str, Any]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        temp.unlink(missing_ok=True)
        inserted = 0
        try:
            connection = self._connect(temp)
            try:
                self._create_schema(connection)
                built_at = datetime.now(UTC).replace(microsecond=0).isoformat()
                metadata = {
                    "schema_version": self.schema_version,
                    "active_index_revision": active_index_revision,
                    "built_at": built_at,
                }
                connection.executemany(
                    "INSERT INTO metadata(key, value) VALUES (?, ?)",
                    sorted(metadata.items()),
                )
                for card in cards:
                    card_id = str(card.get("card_id") or card.get("ID") or "")
                    if not card_id or str(card.get("status") or "active") != "active":
                        continue
                    revision_id = str(card.get("revision_id") or "")
                    title = str(card.get("title") or card.get("summary") or card_id)
                    summary = str(card.get("summary") or "")
                    tags = " ".join(str(item) for item in _items(card.get("tags")))
                    body = _search_text(card)
                    connection.execute(
                        """
                        INSERT INTO cards(
                          card_id, revision_id, card_type, status, evidence_maturity,
                          operator, hardware, toolchain, dtype, layout, parent_family,
                          body_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            card_id,
                            revision_id,
                            str(card.get("card_type") or "MechanismPolicy"),
                            "active",
                            str(card.get("evidence_maturity") or "draft"),
                            _scope_value(card, "operator", "operator_id"),
                            _scope_value(card, "hardware", "architecture", "soc"),
                            _scope_value(card, "toolchain", "cann"),
                            _scope_value(card, "dtype"),
                            _scope_value(card, "layout"),
                            _scope_value(card, "parent_family"),
                            canonical_json(dict(card)),
                        ),
                    )
                    connection.execute(
                        "INSERT INTO cards_fts(card_id, title, summary, body, tags) VALUES (?, ?, ?, ?, ?)",
                        (card_id, title, summary, body, tags),
                    )
                    inserted += 1
                connection.commit()
                integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
                if integrity != "ok":
                    raise sqlite3.DatabaseError(f"integrity_check failed: {integrity}")
            finally:
                connection.close()
            os.replace(temp, self.path)
        except (OSError, sqlite3.Error) as exc:
            temp.unlink(missing_ok=True)
            return {
                "schema_version": self.schema_version,
                "status": "error",
                "code": "READ_MODEL_REBUILD_FAILED",
                "message": str(exc),
                "path": str(self.path),
            }
        digest = "sha256:" + hashlib.sha256(self.path.read_bytes()).hexdigest()
        return {
            "schema_version": self.schema_version,
            "status": "rebuilt",
            "path": str(self.path),
            "card_count": inserted,
            "active_index_revision": active_index_revision,
            "digest": digest,
        }

    def status(self, *, expected_revision: str | None = None) -> dict[str, Any]:
        if not self.path.is_file():
            return {
                "schema_version": self.schema_version,
                "status": "missing",
                "path": str(self.path),
                "fresh": False,
                "card_count": 0,
            }
        try:
            connection = self._connect(self.path)
            try:
                metadata = {
                    row["key"]: row["value"]
                    for row in connection.execute("SELECT key, value FROM metadata")
                }
                count = int(connection.execute("SELECT COUNT(*) FROM cards").fetchone()[0])
                connection.execute("SELECT rowid FROM cards_fts LIMIT 1").fetchall()
            finally:
                connection.close()
        except sqlite3.Error as exc:
            return {
                "schema_version": self.schema_version,
                "status": "corrupt",
                "path": str(self.path),
                "fresh": False,
                "card_count": 0,
                "message": str(exc),
            }
        revision = metadata.get("active_index_revision")
        fresh = expected_revision is None or revision == expected_revision
        return {
            "schema_version": self.schema_version,
            "status": "ready" if fresh else "stale",
            "path": str(self.path),
            "fresh": fresh,
            "card_count": count,
            "active_index_revision": revision,
            "built_at": metadata.get("built_at"),
            "digest": "sha256:" + hashlib.sha256(self.path.read_bytes()).hexdigest(),
        }

    @staticmethod
    def _match_query(text: str) -> str:
        tokens = re.findall(r"[A-Za-z0-9_+.-]+|[\u3400-\u9fff]{2,}", text)
        unique = list(dict.fromkeys(token for token in tokens if token.strip()))[:24]
        return " OR ".join('"' + token.replace('"', '""') + '"' for token in unique)

    def search(
        self,
        text: str,
        *,
        candidate_ids: Sequence[str] | None = None,
        limit: int = 30,
        expected_revision: str | None = None,
    ) -> list[str]:
        state = self.status(expected_revision=expected_revision)
        if state.get("status") != "ready":
            return []
        match = self._match_query(text)
        if not match:
            return []
        candidates = list(dict.fromkeys(str(item) for item in candidate_ids or [] if item))
        sql = "SELECT card_id, bm25(cards_fts) AS rank FROM cards_fts WHERE cards_fts MATCH ?"
        params: list[Any] = [match]
        if candidates:
            placeholders = ",".join("?" for _ in candidates)
            sql += f" AND card_id IN ({placeholders})"
            params.extend(candidates)
        sql += " ORDER BY rank, card_id LIMIT ?"
        params.append(max(1, min(int(limit), 100)))
        try:
            connection = self._connect(self.path)
            try:
                rows = connection.execute(sql, params).fetchall()
            finally:
                connection.close()
        except sqlite3.Error:
            return []
        return [str(row["card_id"]) for row in rows]
