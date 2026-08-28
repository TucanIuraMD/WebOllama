"""Logs endpoints — Web UI / job logs, and optionally Ollama systemd logs."""
import logging
import os
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from ..config import LOG_FILE
from ..deps import require_user

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/logs", tags=["logs"])


def _tail_file(path: str, n: int = 100, search: str = "") -> list[str]:
    path = Path(path)
    if not path.exists():
        return []
    try:
        lines = path.read_text(errors="replace").splitlines()
    except Exception as exc:
        return [f"error reading log: {exc}"]
    if search:
        lines = [l for l in lines if search.lower() in l.lower()]
    lines = lines[-n:]
    return lines


@router.get("/webui")
async def webui_logs(
    tail: int = Query(default=100, le=5000),
    search: str = Query(default=""),
    user: dict = Depends(require_user),
):
    lines = _tail_file(LOG_FILE, tail, search)
    return {"lines": lines, "count": len(lines), "file": LOG_FILE}


@router.get("/job")
async def job_logs(
    job_id: Optional[str] = Query(default=None),
    tail: int = Query(default=200, le=5000),
    user: dict = Depends(require_user),
):
    from ..db import Database

    if job_id:
        job = await Database.get().get_job(job_id)
        if job:
            output = (job.get("output", "") or "").splitlines()
            if tail and tail < len(output):
                output = output[-tail:]
            return {"lines": output, "count": len(output), "job_id": job_id}
        return {"lines": [], "count": 0, "job_id": job_id}
    return {"lines": [], "count": 0, "message": "specify job_id to view job logs"}


@router.get("/ollama")
async def ollama_logs(
    tail: int = Query(default=100, le=5000),
    search: str = Query(default=""),
    user: dict = Depends(require_user),
):
    """Try to read Ollama systemd logs via journalctl (safe subprocess)."""
    try:
        import asyncio

        proc = await asyncio.create_subprocess_exec(
            "journalctl", "-u", "ollama", "--no-pager", "-n", str(tail),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        lines = stdout.decode(errors="replace").splitlines()
        if search:
            lines = [l for l in lines if search.lower() in l.lower()]
        return {"lines": lines, "count": len(lines), "source": "journalctl"}
    except Exception as exc:
        return {"lines": [f"ollama systemd logs not available: {exc}"], "count": 1, "source": "unavailable"}