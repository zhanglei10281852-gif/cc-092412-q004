from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Query, Response

from app.api.dependencies import current_principal
from app.core.errors import NotFoundError
from app.core.pagination import Page, page_result
from app.core.security import Principal
from app.database import get_connection, transaction
from app.repositories.resident_merge import ResidentMergeRepository
from app.schemas.resident_merge import ResidentMergeRequest
from app.services.idempotency import IdempotencyService
from app.services.resident_merge import ResidentMergeService

router = APIRouter(prefix="/api/resident-merges", tags=["居民档案合并"])


@router.get("/preview")
def preview_merge(
    source_id: int = Query(gt=0),
    target_id: int = Query(gt=0),
    principal: Principal = Depends(current_principal),
) -> dict:
    """合并前预览：逐字段差异、双方关联事务与合并阻碍。"""
    return ResidentMergeService(get_connection()).preview(principal, source_id, target_id)


@router.post("", status_code=201)
def merge_residents(
    data: ResidentMergeRequest,
    response: Response,
    idempotency_key: str | None = Header(default=None),
    principal: Principal = Depends(current_principal),
) -> dict:
    """执行受控合并：字段决策、关联迁移、源档案转合并记录在同一事务内完成。"""
    payload = data.model_dump()
    with transaction(immediate=True) as connection:
        service = ResidentMergeService(connection)

        def operation() -> tuple[dict, int]:
            record = service.merge(
                principal,
                data.source_id,
                data.target_id,
                data.take_from_source,
                reason=data.reason,
                idempotency_key=idempotency_key,
            )
            return record, 201

        if idempotency_key:
            stored = IdempotencyService(connection).execute("resident.merge", idempotency_key, payload, operation)
            if stored.replayed:
                response.headers["X-Idempotent-Replay"] = "true"
            return stored.body
        record, _ = operation()
        return record


@router.get("")
def list_merges(
    source_id: int | None = None,
    target_id: int | None = None,
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    principal: Principal = Depends(current_principal),
) -> dict:
    principal.require("residents.read")
    pagination = Page(page, size)
    repository = ResidentMergeRepository(get_connection())
    rows = repository.list(source_id=source_id, target_id=target_id, limit=size, offset=pagination.offset)
    total = repository.count(source_id=source_id, target_id=target_id)
    return page_result(total=total, page=pagination, rows=rows)


@router.get("/{merge_id}")
def get_merge(merge_id: int, principal: Principal = Depends(current_principal)) -> dict:
    principal.require("residents.read")
    record = ResidentMergeRepository(get_connection()).get(merge_id)
    if record is None:
        raise NotFoundError("合并记录不存在")
    return record
