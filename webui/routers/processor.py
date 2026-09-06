"""Processor endpoints — CPU/GPU/Ollama stats of the machine running WebOllama.

GET /api/system/processor returns LOCAL host data (the deployment target is
the Ollama server itself): GPU via the existing GPUCollector, CPU/RAM via
the existing SystemCollector, running models via the existing OllamaClient
(/api/ps, i.e. the data behind `ollama ps`, with a local CLI fallback).

It deliberately does not reuse /api/status, whose payload serves the live
WebSocket snapshot format.
"""
import logging

from fastapi import APIRouter, Depends

from ..deps import require_user
from ..remote_sys import get_processor_collector

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/system", tags=["processor"])


@router.get("/processor")
async def processor(user: dict = Depends(require_user)):
    """Local CPU/GPU/VRAM/temperature + running Ollama models.

    Always 200: unavailable pieces are structured JSON
    ({"available": false, ...}, gpu_available, ollama.online) — never a
    traceback, regardless of missing GPU, missing ollama CLI or offline
    Ollama API.
    """
    return await get_processor_collector().sample()
