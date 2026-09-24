from __future__ import annotations

import json
import sqlite3
from typing import Any

from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.security import Principal
from app.repositories.base import row_dict, rows_dict
from app.schemas.business import MERGEABLE_RESIDENT_FIELDS
from app.services.audit import AuditContext, AuditService
from app.services.idempotency import IdempotencyService, StoredResponse

IDEMPOTENCY_SCOPE = "resident-merge"

FIELD_LABELS = {
    "name": "姓名",
    "id_card": "身份证号",
    "phone": "联系电话",
    "address": "住址",
    "village": "所属村",
    "household_head": "户主",
    "gender": "性别",
    "birth_date": "出生日期",
}
PREVIEW_FIELDS = tuple(MERGEABLE_RESIDENT_FIELDS) + ("gender", "birth_date")


def follow_merge_chain(connection: sqlite3.Connection, resident_id: int) -> list[dict[str, Any]]:
    """从给定档案出发沿 merged_into_id 走到主档案，返回完整链条（含起点与终点）。"""
    chain: list[dict[str, Any]] = []
    seen: set[int] = set()
    current_id: int | None = resident_id
    while current_id is not None:
        if current_id in seen:
            raise ConflictError("合并链存在循环，数据异常", context={"resident_id": resident_id})
        seen.add(current_id)
        row = row_dict(connection.execute(
            "SELECT id,name,status,merged_into_id,merged_at FROM residents WHERE id=?", (current_id,)
        ).fetchone())
        if row is None:
            raise ConflictError("合并链引用了不存在的档案，数据异常", context={"resident_id": resident_id, "missing_id": current_id})
        chain.append(row)
        current_id = row["merged_into_id"] if row["status"] == "merged" else None
    return chain


class ResidentMergeService:
    def __init__(self, connection: sqlite3.Connection, clock: Clock | None = None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        self.audit = AuditService(connection, self.clock)
        self.idempotency = IdempotencyService(connection, self.clock)

    # ---------- 差异预览 ----------

    def preview(self, principal: Principal, source_id: int, target_id: int) -> dict:
        principal.require("residents.read")
        source = self._require_resident(source_id)
        target = self._require_resident(target_id)
        fields = [
            {
                "field": field,
                "label": FIELD_LABELS[field],
                "source_value": source.get(field),
                "target_value": target.get(field),
                "different": (source.get(field) or None) != (target.get(field) or None),
                "decidable": field in MERGEABLE_RESIDENT_FIELDS,
            }
            for field in PREVIEW_FIELDS
        ]
        blockers = self._blockers(source, target)
        return {
            "source": source,
            "target": target,
            "fields": fields,
            "source_affairs": self._affairs_of(source_id),
            "target_affairs": self._affairs_of(target_id),
            "mergeable": not blockers,
            "blockers": blockers,
        }

    # ---------- 执行合并 ----------

    def execute(self, principal: Principal, payload: dict) -> StoredResponse:
        principal.require("residents.merge")
        normalized = {**payload, "take_from_source": sorted(set(payload.get("take_from_source") or []))}
        return self.idempotency.execute(
            IDEMPOTENCY_SCOPE,
            normalized["idempotency_key"],
            normalized,
            lambda: (self._apply_merge(principal, normalized), 201),
        )

    def _apply_merge(self, principal: Principal, payload: dict) -> dict:
        source_id = int(payload["source_id"])
        target_id = int(payload["target_id"])
        take_from_source: list[str] = payload["take_from_source"]
        reason = (payload.get("reason") or "").strip()
        now = to_storage(self.clock.now())

        source = self._require_resident(source_id)
        target = self._require_resident(target_id)
        blockers = self._blockers(source, target)
        if blockers:
            raise ConflictError("；".join(blockers), context={"source_id": source_id, "target_id": target_id})
        if source_id in {row["id"] for row in follow_merge_chain(self.connection, target_id)}:
            raise ConflictError("合并会形成循环，已拒绝", context={"source_id": source_id, "target_id": target_id})

        # 逐项字段覆盖：仅 take_from_source 中列出的字段从源档案取值
        changes: list[dict[str, Any]] = []
        assignments: list[str] = []
        params: list[Any] = []
        for field in take_from_source:
            before, after = target.get(field), source.get(field)
            if before == after:
                continue
            changes.append({"field": field, "label": FIELD_LABELS[field], "before": before, "after": after})
            assignments.append(f"{field}=?")
            params.append(after)

        # 证件号随主档案时，源档案先改留墓碑值释放唯一约束，原号码保存在合并记录中
        source_id_card = source["id_card"]
        id_card_moves = any(change["field"] == "id_card" for change in changes)
        retired_id_card = f"MERGED-{source_id}" if id_card_moves else source_id_card
        if id_card_moves:
            self.connection.execute("UPDATE residents SET id_card=? WHERE id=?", (retired_id_card, source_id))

        if assignments:
            self.connection.execute(
                f"UPDATE residents SET {','.join(assignments)},updated_at=? WHERE id=?",
                (*params, now, target_id),
            )

        # 事务关联迁移：源档案名下全部事务在同一事务内转到主档案
        affair_ids = [int(row["id"]) for row in self.connection.execute(
            "SELECT id FROM affairs WHERE applicant_id=? ORDER BY id", (source_id,)
        ).fetchall()]
        if affair_ids:
            self.connection.execute(
                "UPDATE affairs SET applicant_id=?,updated_at=? WHERE applicant_id=?",
                (target_id, now, source_id),
            )

        # 源档案转为合并记录：CAS 状态位，并发下只有一方能成功
        cursor = self.connection.execute(
            "UPDATE residents SET status='merged',merged_into_id=?,merged_at=?,id_card=?,updated_at=? "
            "WHERE id=? AND status='active'",
            (target_id, now, retired_id_card, now, source_id),
        )
        if cursor.rowcount != 1:
            raise ConflictError("源档案已被其他合并操作处理", context={"source_id": source_id})

        cursor = self.connection.execute(
            "INSERT INTO resident_merges(source_id,target_id,source_id_card,reason,field_changes_json,"
            "moved_affairs_json,operator_user_id,operator_name,idempotency_key,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                source_id, target_id, source_id_card, reason,
                _json_dumps(changes), _json_dumps(affair_ids),
                principal.user_id, principal.display_name, payload["idempotency_key"], now,
            ),
        )
        merge_id = int(cursor.lastrowid)
        correlation_id = f"resident-merge:{merge_id}"
        context = AuditContext(principal.user_id, principal.display_name, correlation_id)

        self.audit.record(
            context,
            action="resident.merge.fields",
            resource_type="resident",
            resource_id=target_id,
            before={change["field"]: change["before"] for change in changes},
            after={change["field"]: change["after"] for change in changes},
            metadata={"merge_id": merge_id, "source_id": source_id, "reason": reason, "take_from_source": take_from_source},
        )
        self.audit.record(
            context,
            action="resident.merge.retire",
            resource_type="resident",
            resource_id=source_id,
            before={"status": "active", "id_card": source_id_card, "merged_into_id": None},
            after={"status": "merged", "id_card": retired_id_card, "merged_into_id": target_id},
            metadata={"merge_id": merge_id, "target_id": target_id, "reason": reason, "moved_affair_ids": affair_ids},
        )
        for affair_id in affair_ids:
            self.audit.record(
                context,
                action="resident.merge.move_affair",
                resource_type="affair",
                resource_id=affair_id,
                before={"applicant_id": source_id},
                after={"applicant_id": target_id},
                metadata={"merge_id": merge_id, "reason": reason},
            )

        return {
            "merge_id": merge_id,
            "source_id": source_id,
            "target_id": target_id,
            "canonical_id": target_id,
            "field_changes": changes,
            "moved_affair_ids": affair_ids,
            "source_status": "merged",
            "created_at": now,
        }

    # ---------- 合并记录与追溯 ----------

    def list_merges(self, principal: Principal, *, source_id: int | None, target_id: int | None, limit: int, offset: int) -> tuple[int, list[dict]]:
        principal.require("residents.read")
        conditions: list[str] = []
        params: list[Any] = []
        for column, value in (("source_id", source_id), ("target_id", target_id)):
            if value is not None:
                conditions.append(f"{column}=?")
                params.append(value)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        total = int(self.connection.execute("SELECT COUNT(*) FROM resident_merges" + where, tuple(params)).fetchone()[0])
        rows = rows_dict(self.connection.execute(
            "SELECT * FROM resident_merges" + where + " ORDER BY id DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall())
        return total, [self._present(row) for row in rows]

    def merge_detail(self, principal: Principal, merge_id: int) -> dict:
        principal.require("residents.read")
        row = row_dict(self.connection.execute("SELECT * FROM resident_merges WHERE id=?", (merge_id,)).fetchone())
        if row is None:
            raise NotFoundError("合并记录不存在")
        return self._present(row)

    def resolve(self, principal: Principal, *, resident_id: int | None, id_card: str | None) -> dict:
        principal.require("residents.read")
        if (resident_id is None) == (id_card is None):
            raise ValidationError("resident_id 与 id_card 必须且只能提供一个")
        if resident_id is not None:
            start = self._require_resident(resident_id)
        else:
            start = row_dict(self.connection.execute("SELECT * FROM residents WHERE id_card=?", (id_card,)).fetchone())
            if start is None:
                merge = row_dict(self.connection.execute(
                    "SELECT * FROM resident_merges WHERE source_id_card=? ORDER BY id DESC LIMIT 1", (id_card,)
                ).fetchone())
                if merge is None:
                    raise NotFoundError("未找到该证件号对应的档案")
                start = self._require_resident(int(merge["source_id"]))
        chain = follow_merge_chain(self.connection, int(start["id"]))
        canonical = chain[-1]
        return {
            "chain": chain,
            "canonical_id": canonical["id"],
            "canonical": self._require_resident(int(canonical["id"])),
        }

    # ---------- 内部工具 ----------

    def _require_resident(self, resident_id: int) -> dict:
        row = row_dict(self.connection.execute("SELECT * FROM residents WHERE id=?", (resident_id,)).fetchone())
        if row is None:
            raise NotFoundError("居民档案不存在", context={"resident_id": resident_id})
        return row

    def _blockers(self, source: dict, target: dict) -> list[str]:
        blockers: list[str] = []
        if source["id"] == target["id"]:
            blockers.append("源档案与目标档案相同")
        if source["status"] != "active":
            blockers.append("源档案已合并，不能重复合并")
        if target["status"] != "active":
            canonical = follow_merge_chain(self.connection, int(target["id"]))[-1]
            blockers.append(f"目标档案已合并停用，请以主档案 #{canonical['id']} 为合并目标")
        return blockers

    def _affairs_of(self, resident_id: int) -> list[dict]:
        return rows_dict(self.connection.execute(
            "SELECT id,title,category,status,created_at FROM affairs WHERE applicant_id=? ORDER BY id",
            (resident_id,),
        ).fetchall())

    def _present(self, row: dict) -> dict:
        presented = dict(row)
        presented["field_changes"] = json.loads(presented.pop("field_changes_json"))
        presented["moved_affair_ids"] = json.loads(presented.pop("moved_affairs_json"))
        return presented


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)
