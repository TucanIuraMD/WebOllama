"""Settings endpoints — read/write UI configuration."""
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from ..audit import get_audit
from ..config import settings_dict
from ..db import Database
from ..deps import current_user, rate_limit_dangerous

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/settings", tags=["settings"])


@router.get("")
async def get_settings(user: dict = Depends(current_user)):
    db = Database.get()
    overrides = await db.get_all_settings()
    base = settings_dict()
    base["overrides"] = overrides
    return base


@router.put("", dependencies=[Depends(rate_limit_dangerous)])
async def update_settings(
    payload: dict,
    user: dict = Depends(current_user),
):
    if user["role"] != "admin":
        raise HTTPException(403, "admin required")
    db = Database.get()
    allowed = {"ollama_url", "sysinfo_url", "webui_host", "webui_port", "refresh_interval",
               "metrics_interval", "history_retention", "gpu_enabled", "log_level"}
    updated = []
    for key, value in payload.items():
        if key not in allowed:
            continue
        await db.set_setting(key, str(value))
        updated.append(key)
    await get_audit().log(user["username"], "update_settings", ",".join(updated))
    return {"ok": True, "updated": updated}