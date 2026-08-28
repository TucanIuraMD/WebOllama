"""Audit log endpoints."""
import logging
from fastapi import APIRouter, Depends, HTTPException, Query

from ..audit import get_audit
from ..deps import current_user

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/audit", tags=["audit"])


@router.get("")
async def list_audit(
    limit: int = Query(default=100, le=500),
    offset: int = Query(default=0, ge=0),
    username: str = Query(default=""),
    action: str = Query(default=""),
    user: dict = Depends(current_user),
):
    if user["role"] != "admin":
        raise HTTPException(403, "admin required")
    rows = await get_audit().list(limit=limit, offset=offset, username=username, action=action)
    return {"rows": rows, "count": len(rows)}