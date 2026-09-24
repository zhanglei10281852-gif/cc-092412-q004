from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from app.api.dependencies import current_principal
from app.core.pagination import Page, page_result
from app.core.security import Principal
from app.database import get_connection, transaction
from app.schemas.business import ResidentMergeRequest
from app.services.merge import ResidentMergeService

router = APIRouter(prefix="/api/resident-merges", tags=["居民档案合并"])


@router.get("/preview")
def preview_merge(
    source_id: int = Query(gt=0),
    target_id: int = Query(gt=0),
    principal: Principal = Depends(current_principal),
) -> dict:
    return ResidentMergeService(get_connection()).preview(principal, source_id, target_id)


@router.get("/resolve")
def resolve_archive(
    resident_id: int | None = Query(default=None, gt=0),
    id_card: str | None = Query(default=None, min_length=1, max_length=64),
    principal: Principal = Depends(current_principal),
) -> dict:
    return ResidentMergeService(get_connection()).resolve(principal, resident_id=resident_id, id_card=id_card)


@router.get("")
def list_merges(
    source_id: int | None = Query(default=None, gt=0),
    target_id: int | None = Query(default=None, gt=0),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    principal: Principal = Depends(current_principal),
) -> dict:
    pagination = Page(page, size)
    total, rows = ResidentMergeService(get_connection()).list_merges(
        principal, source_id=source_id, target_id=target_id, limit=size, offset=pagination.offset
    )
    return page_result(total=total, page=pagination, rows=rows)


@router.get("/{merge_id}")
def merge_detail(merge_id: int, principal: Principal = Depends(current_principal)) -> dict:
    return ResidentMergeService(get_connection()).merge_detail(principal, merge_id)


@router.post("", status_code=201)
def execute_merge(data: ResidentMergeRequest, principal: Principal = Depends(current_principal)) -> JSONResponse:
    with transaction(immediate=True) as connection:
        stored = ResidentMergeService(connection).execute(principal, data.model_dump())
    return JSONResponse(status_code=stored.status_code, content={**stored.body, "replayed": stored.replayed})
