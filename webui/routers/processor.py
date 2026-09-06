"""Processor endpoints — CPU/GPU statistics of the remote Ollama server.

GET /api/system/processor returns the live stats of the machine that runs
Ollama (192.168.80.22), collected via the optional host-agent (remote_sys.py).
It deliberately does NOT reuse /api/status or /api/system, which describe the
local WebOllama host (.111) and must keep their current meaning.
"""
import logging

from fastapi import APIRouter, Depends

from ..deps import require_user
from ..remote_sys import get_remote_processor

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/system", tags=["processor"])


@router.get("/processor")
async def processor(user: dict = Depends(require_user)):
    """CPU/GPU/VRAM/temperature of the remote Ollama host.

    Always 200: unavailable state is structured JSON
    ({"available": false, "reason": ...}), never a traceback.
    """
    return await get_remote_processor().sample()
