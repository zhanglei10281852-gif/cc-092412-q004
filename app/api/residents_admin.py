from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.dependencies import current_principal
from app.core.security import Principal
from app.database import get_connection, transaction
from app.services.resident_merge import ResidentMergeService

router = APIRouter(prefix="/api/residents", tags=["居民档案管理"])


@router.get("/{resident_id}/master")
def get_master(resident_id: int, principal: Principal = Depends(current_principal)) -> dict:
    """旧编号追溯：沿合并链找到当前可办理业务的主档案。"""
    principal.require("residents.read")
    return ResidentMergeService(get_connection()).master_chain(resident_id)


@router.post("/{resident_id}/disable")
def disable_resident(resident_id: int, principal: Principal = Depends(current_principal)) -> dict:
    with transaction(immediate=True) as connection:
        return ResidentMergeService(connection).disable(principal, resident_id)


@router.post("/{resident_id}/enable")
def enable_resident(resident_id: int, principal: Principal = Depends(current_principal)) -> dict:
    with transaction(immediate=True) as connection:
        return ResidentMergeService(connection).enable(principal, resident_id)
