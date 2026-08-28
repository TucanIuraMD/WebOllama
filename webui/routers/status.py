"""Status / GPU / System / history endpoints."""
import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Query

from ..config import HISTORY_RETENTION
from ..db import Database
from ..deps import require_user
from ..gpu_collector import get_gpu_collector
from ..ollama_client import get_client
from ..realtime import get_realtime_service
from ..sys_collector import get_system_collector

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["status"])


@router.get("/status")
async def status(user: dict = Depends(require_user)):
    rt = get_realtime_service()
    if rt.last_snapshot:
        return rt.last_snapshot
    return await rt.build_snapshot()


@router.get("/gpu")
async def gpu(user: dict = Depends(require_user)):
    return await get_gpu_collector().sample()


@router.get("/gpu/processes")
async def gpu_processes(user: dict = Depends(require_user)):
    return await get_gpu_collector().gpu_processes()


@router.get("/system")
async def system(user: dict = Depends(require_user)):
    return await get_system_collector().sample()


@router.get("/ollama/status")
async def ollama_status(user: dict = Depends(require_user)):
    return await get_client().status()


@router.get("/history/{kind}")
async def history(
    kind: str,
    minutes: int = Query(default=60, ge=1, le=1440),
    user: dict = Depends(require_user),
):
    """Metric history for charts. kinds: gpu, system."""
    db = Database.get()
    since = time.time() - minutes * 60
    rows = await db.get_metrics(kind, since)
    return {"kind": kind, "minutes": minutes, "points": rows, "retention": HISTORY_RETENTION}
