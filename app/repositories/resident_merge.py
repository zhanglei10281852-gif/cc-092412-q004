from __future__ import annotations

import json
import sqlite3
from typing import Any

from app.repositories.base import row_dict, rows_dict

JSON_COLUMNS = ("source_snapshot", "target_before", "target_after", "field_decisions", "moved_affairs")


def _decode(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    for column in JSON_COLUMNS:
        row[column] = json.loads(row[f"{column}_json"])
        row.pop(f"{column}_json", None)
    return row


class ResidentMergeRepository:
    """合并记录表 resident_merges 的读写。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def insert(
        self,
        *,
        source_id: int,
        target_id: int,
        source_snapshot: dict,
        target_before: dict,
        target_after: dict,
        field_decisions: dict,
        moved_affairs: list[dict],
        reason: str | None,
        operator_user_id: int | None,
        operator_name: str,
        idempotency_key: str | None,
        created_at: str,
    ) -> int:
        cursor = self.connection.execute(
            "INSERT INTO resident_merges(source_id,target_id,source_snapshot_json,target_before_json,target_after_json,"
            "field_decisions_json,moved_affairs_json,reason,operator_user_id,operator_name,idempotency_key,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                source_id,
                target_id,
                json.dumps(source_snapshot, ensure_ascii=False, sort_keys=True),
                json.dumps(target_before, ensure_ascii=False, sort_keys=True),
                json.dumps(target_after, ensure_ascii=False, sort_keys=True),
                json.dumps(field_decisions, ensure_ascii=False, sort_keys=True),
                json.dumps(moved_affairs, ensure_ascii=False, sort_keys=True),
                reason,
                operator_user_id,
                operator_name,
                idempotency_key,
                created_at,
            ),
        )
        return int(cursor.lastrowid)

    def get(self, merge_id: int) -> dict[str, Any] | None:
        return _decode(row_dict(self.connection.execute(
            "SELECT * FROM resident_merges WHERE id=?", (merge_id,)
        ).fetchone()))

    def find_by_idempotency_key(self, key: str) -> dict[str, Any] | None:
        return _decode(row_dict(self.connection.execute(
            "SELECT * FROM resident_merges WHERE idempotency_key=?", (key,)
        ).fetchone()))

    def list(self, *, source_id: int | None, target_id: int | None, limit: int, offset: int) -> list[dict]:
        conditions: list[str] = []
        params: list[Any] = []
        if source_id is not None:
            conditions.append("source_id=?")
            params.append(source_id)
        if target_id is not None:
            conditions.append("target_id=?")
            params.append(target_id)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        params.extend([limit, offset])
        return [_decode(row) for row in rows_dict(self.connection.execute(
            "SELECT * FROM resident_merges" + where + " ORDER BY id DESC LIMIT ? OFFSET ?", tuple(params)
        ).fetchall())]

    def count(self, *, source_id: int | None, target_id: int | None) -> int:
        conditions: list[str] = []
        params: list[Any] = []
        if source_id is not None:
            conditions.append("source_id=?")
            params.append(source_id)
        if target_id is not None:
            conditions.append("target_id=?")
            params.append(target_id)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        return int(self.connection.execute("SELECT COUNT(*) FROM resident_merges" + where, tuple(params)).fetchone()[0])
