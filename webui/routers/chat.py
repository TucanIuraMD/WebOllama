"""Chat endpoint — proxy to Ollama native chat API."""
import logging
from fastapi import APIRouter, Depends, HTTPException
from ..audit import get_audit
from ..deps import rate_limit_dangerous, require_user
from ..ollama_client import OllamaClient, OllamaError, get_client

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/chat", tags=["chat"])

@router.post("/run", dependencies=[Depends(rate_limit_dangerous)])
async def run_chat(payload: dict, user: dict = Depends(require_user)):
    model = str(payload.get("model", "")).strip()
    messages = payload.get("messages", [])
    if not model:
        raise HTTPException(400, "model is required")
    if not isinstance(messages, list) or not messages:
        raise HTTPException(400, "messages must be a non-empty list")
    client = get_client()
    try:
        resp = await client.chat(model, messages, stream=False)
    except OllamaError as exc:
        raise HTTPException(502, detail=exc.message)
    message = resp.get("message", {})
    await get_audit().log(user["username"], "chat", model)
    return {
        "ok": True,
        "message": {
            "role": message.get("role", "assistant"),
            "content": message.get("content", ""),
        },
        "model": resp.get("model", model),
        "done": resp.get("done", True),
        "total_duration": resp.get("total_duration"),
        "prompt_eval_count": resp.get("prompt_eval_count"),
        "eval_count": resp.get("eval_count"),
    }
