from __future__ import annotations

import sqlite3

from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.security import Principal
from app.repositories.resident_merge import ResidentMergeRepository
from app.services.audit import AuditContext, AuditService

# 允许在合并时逐项决定覆盖的档案字段
MERGEABLE_FIELDS = ("name", "id_card", "gender", "birth_date", "phone", "address", "village", "household_head")

RESIDENT_PUBLIC_FIELDS = (
    "id", "name", "id_card", "gender", "birth_date", "phone", "address", "village",
    "household_head", "status", "merged_into_id", "merged_at", "created_at", "updated_at",
)


def _resident_public(row: dict) -> dict:
    return {key: row.get(key) for key in RESIDENT_PUBLIC_FIELDS}


def resolve_master_id(connection: sqlite3.Connection, resident_id: int) -> int:
    """沿 merged_into_id 链找到最终主档案；链上存在环时拒绝并抛出冲突。"""
    visited: set[int] = set()
    current_id = resident_id
    while True:
        row = connection.execute(
            "SELECT id,status,merged_into_id FROM residents WHERE id=?", (current_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("居民不存在")
        if row["status"] != "merged" or row["merged_into_id"] is None:
            return int(row["id"])
        if current_id in visited:
            raise ConflictError("合并链存在循环，请先修复档案数据")
        visited.add(current_id)
        current_id = int(row["merged_into_id"])


class ResidentMergeService:
    """居民档案的受控合并与生命周期管理。

    合并操作必须在调用方开启的即时事务中执行：校验、字段覆盖、
    事务关联迁移、源档案转合并记录与审计留痕要么全部提交，要么整体回滚。
    """

    def __init__(self, connection: sqlite3.Connection, clock: Clock | None = None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        self.merges = ResidentMergeRepository(connection)
        self.audit = AuditService(connection, self.clock)

    # ---------- 查询 ----------

    def preview(self, principal: Principal, source_id: int, target_id: int) -> dict:
        principal.require("residents.read")
        if source_id == target_id:
            raise ValidationError("源档案与目标档案不能相同")
        source = self._require(source_id)
        target = self._require(target_id)
        fields = [
            {
                "field": field,
                "source_value": source[field],
                "target_value": target[field],
                "differs": source[field] != target[field],
            }
            for field in MERGEABLE_FIELDS
        ]
        blockers = self._merge_blockers(source, target)
        return {
            "source": _resident_public(source),
            "target": _resident_public(target),
            "fields": fields,
            "source_affairs": self._affairs_of(source_id),
            "target_affairs": self._affairs_of(target_id),
            "blockers": blockers,
            "mergeable": not blockers,
        }

    def resolve_master(self, resident_id: int) -> int:
        return resolve_master_id(self.connection, resident_id)

    def master_chain(self, resident_id: int) -> dict:
        """返回旧编号沿合并链追溯到主档案的完整路径。"""
        chain: list[int] = []
        current = self._require(resident_id)
        while current["status"] == "merged" and current["merged_into_id"] is not None:
            if current["id"] in chain:
                raise ConflictError("合并链存在循环，请先修复档案数据")
            chain.append(int(current["id"]))
            current = self._require(int(current["merged_into_id"]))
        chain.append(int(current["id"]))
        return {"resident_id": resident_id, "master_id": int(current["id"]), "chain": chain, "master": _resident_public(current)}

    # ---------- 合并 ----------

    def merge(
        self,
        principal: Principal,
        source_id: int,
        target_id: int,
        take_from_source: list[str],
        reason: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict:
        principal.require("residents.merge")
        if source_id == target_id:
            raise ValidationError("源档案与目标档案不能相同")
        unknown = sorted(set(take_from_source) - set(MERGEABLE_FIELDS))
        if unknown:
            raise ValidationError(f"字段不可合并：{','.join(unknown)}")
        take = list(dict.fromkeys(take_from_source))

        source = self._require(source_id)
        target = self._require(target_id)
        blockers = self._merge_blockers(source, target)
        if blockers:
            raise ConflictError("；".join(blockers), context={
                "source_id": source_id,
                "target_id": target_id,
                "source_status": source["status"],
                "target_status": target["status"],
            })

        now = to_storage(self.clock.now())

        # 逐项字段决策：默认保留主档案，仅列出的字段从源档案覆盖
        decisions: dict[str, dict] = {}
        assignments: dict[str, object] = {}
        for field in MERGEABLE_FIELDS:
            use_source = field in take
            decisions[field] = {
                "decision": "take_source" if use_source else "keep_target",
                "source_value": source[field],
                "target_before": target[field],
                "target_after": source[field] if use_source else target[field],
            }
            if use_source:
                assignments[field] = source[field]

        # 证件号被主档案采用时，先把源档案的证件号改为占位值，避免违反唯一约束
        source_id_card_tombstone = None
        if "id_card" in assignments and assignments["id_card"] != target["id_card"]:
            source_id_card_tombstone = f"MERGED#{source_id}"

        # 1) 源档案转为合并记录（必要时先腾退证件号）
        source_updates = ["status='merged'", "merged_into_id=?", "merged_at=?", "updated_at=?"]
        source_params: list = [target_id, now, now]
        if source_id_card_tombstone is not None:
            source_updates.append("id_card=?")
            source_params.append(source_id_card_tombstone)
        source_params.append(source_id)
        self.connection.execute(
            f"UPDATE residents SET {','.join(source_updates)} WHERE id=?", tuple(source_params)
        )

        # 2) 主档案按决策覆盖字段
        if assignments:
            set_clause = ",".join(f"{field}=?" for field in assignments)
            self.connection.execute(
                f"UPDATE residents SET {set_clause},updated_at=? WHERE id=?",
                (*assignments.values(), now, target_id),
            )

        # 3) 事务关联在同一事务内迁移到主档案
        moved_affairs = [
            {"id": int(row["id"]), "title": row["title"], "status": row["status"]}
            for row in self.connection.execute(
                "SELECT id,title,status FROM affairs WHERE applicant_id=? ORDER BY id", (source_id,)
            ).fetchall()
        ]
        if moved_affairs:
            self.connection.execute(
                "UPDATE affairs SET applicant_id=?,updated_at=? WHERE applicant_id=?",
                (target_id, now, source_id),
            )

        target_after = self._require(target_id)
        source_after = self._require(source_id)

        # 4) 合并记录：双方快照 + 逐字段决策 + 迁移明细，可完整还原
        merge_id = self.merges.insert(
            source_id=source_id,
            target_id=target_id,
            source_snapshot=_resident_public(source),
            target_before=_resident_public(target),
            target_after=_resident_public(target_after),
            field_decisions=decisions,
            moved_affairs=moved_affairs,
            reason=reason,
            operator_user_id=principal.user_id,
            operator_name=principal.display_name,
            idempotency_key=idempotency_key,
            created_at=now,
        )

        # 5) 审计：目标字段变化、源状态变化、关联迁移各一条，可用 merge_id 串联
        context = AuditContext(principal.user_id, principal.display_name, correlation_id=f"resident-merge:{merge_id}")
        changed_fields = {
            field: {"before": target[field], "after": target_after[field]}
            for field in MERGEABLE_FIELDS
            if target[field] != target_after[field]
        }
        self.audit.record(
            context,
            action="resident.merge",
            resource_type="resident",
            resource_id=target_id,
            before={field: values["before"] for field, values in changed_fields.items()},
            after={field: values["after"] for field, values in changed_fields.items()},
            metadata={
                "merge_id": merge_id,
                "role": "target",
                "source_id": source_id,
                "take_from_source": take,
                "moved_affair_ids": [item["id"] for item in moved_affairs],
                "reason": reason,
            },
        )
        self.audit.record(
            context,
            action="resident.merge",
            resource_type="resident",
            resource_id=source_id,
            before={"status": source["status"], "id_card": source["id_card"], "merged_into_id": None},
            after={"status": "merged", "id_card": source_after["id_card"], "merged_into_id": target_id},
            metadata={"merge_id": merge_id, "role": "source", "target_id": target_id, "reason": reason},
        )
        if moved_affairs:
            self.audit.record(
                context,
                action="resident.merge.affairs",
                resource_type="resident",
                resource_id=source_id,
                before={"affair_ids": [item["id"] for item in moved_affairs]},
                after={"affair_ids": []},
                metadata={"merge_id": merge_id, "target_id": target_id},
            )

        record = self.merges.get(merge_id)
        assert record is not None
        return record

    # ---------- 停用 / 启用 ----------

    def disable(self, principal: Principal, resident_id: int) -> dict:
        return self._set_status(principal, resident_id, "disabled")

    def enable(self, principal: Principal, resident_id: int) -> dict:
        return self._set_status(principal, resident_id, "active")

    def _set_status(self, principal: Principal, resident_id: int, status: str) -> dict:
        principal.require("residents.write")
        resident = self._require(resident_id)
        if resident["status"] == "merged":
            raise ConflictError("合并记录不可再变更状态")
        if resident["status"] == status:
            raise ConflictError("居民档案已处于该状态")
        now = to_storage(self.clock.now())
        self.connection.execute(
            "UPDATE residents SET status=?,updated_at=? WHERE id=?", (status, now, resident_id)
        )
        after = self._require(resident_id)
        self.audit.record(
            AuditContext(principal.user_id, principal.display_name),
            action="resident.disable" if status == "disabled" else "resident.enable",
            resource_type="resident",
            resource_id=resident_id,
            before={"status": resident["status"]},
            after={"status": status},
        )
        return _resident_public(after)

    # ---------- 内部 ----------

    def _require(self, resident_id: int) -> dict:
        row = self.connection.execute("SELECT * FROM residents WHERE id=?", (resident_id,)).fetchone()
        if row is None:
            raise NotFoundError("居民不存在")
        return dict(row)

    def _affairs_of(self, resident_id: int) -> list[dict]:
        return [
            {"id": int(row["id"]), "title": row["title"], "category": row["category"], "status": row["status"]}
            for row in self.connection.execute(
                "SELECT id,title,category,status FROM affairs WHERE applicant_id=? ORDER BY id", (resident_id,)
            ).fetchall()
        ]

    def _merge_blockers(self, source: dict, target: dict) -> list[str]:
        blockers: list[str] = []
        if source["status"] == "merged":
            blockers.append("源档案已是合并记录，不能重复合并")
        if target["status"] != "active":
            blockers.append("目标档案已停用或已合并，不能作为合并目标")
        if self._chain_contains(target["id"], source["id"]):
            blockers.append("合并会形成循环引用")
        return blockers

    def _chain_contains(self, start_id: int, wanted_id: int) -> bool:
        """从 start_id 沿合并链向上走，若遇到 wanted_id 说明合并会成环。"""
        visited: set[int] = set()
        current_id: int | None = start_id
        while current_id is not None:
            if current_id == wanted_id:
                return True
            if current_id in visited:
                return True  # 已有数据成环，同样拒绝
            visited.add(current_id)
            row = self.connection.execute(
                "SELECT merged_into_id FROM residents WHERE id=?", (current_id,)
            ).fetchone()
            current_id = row["merged_into_id"] if row else None
        return False
