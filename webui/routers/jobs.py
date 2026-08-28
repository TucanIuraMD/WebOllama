"""Jobs endpoints."""
import logging

from fastapi import APIRouter, Depends, HTTPException, Query

from ..deps import require_user
from ..jobs import get_job_manager

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/jobs", tags=["jobs"])


@router.get("")
async def list_jobs(limit: int = Query(default=50, le=200), user: dict = Depends(require_user)):
    return {"jobs": await get_job_manager().list(limit=limit)}


@router.get("/{job_id}")
async def get_job(job_id: str, user: dict = Depends(require_user)):
    job = await get_job_manager().get(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    return job


@router.post("/{job_id}/cancel")
async def cancel_job(job_id: str, user: dict = Depends(require_user)):
    job = await get_job_manager().cancel(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    return {"ok": True, "job": job}
