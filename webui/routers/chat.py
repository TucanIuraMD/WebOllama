"""Chat endpoints — proxy to Ollama native chat API.

/run   : blocking single-shot completion (kept for API compatibility)
/stream: Server-Sent Events proxy over Ollama's NDJSON chat stream, so the
         UI renders tokens incrementally instead of waiting for the whole
         answer. SSE was chosen over raw NDJSON passthrough because it is
         natively consumable from the browser (fetch + ReadableStream
         parsing on our side keeps the client dependency-free).
"""
import asyncio
import json
import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from ..audit import get_audit
from ..deps import rate_limit_dangerous, require_user
from ..ollama_client import OllamaClient, OllamaError, get_client

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/chat", tags=["chat"])


def _validate_payload(payload: dict) -> tuple[str, list[dict], dict | None]:
    model = str(payload.get("model", "")).strip()
    messages = payload.get("messages", [])
    if not model:
        raise HTTPException(400, "model is required")
    if not isinstance(messages, list) or not messages:
        raise HTTPException(400, "messages must be a non-empty list")
    for m in messages:
        if not isinstance(m, dict) or not isinstance(m.get("content"), str):
            raise HTTPException(400, "each message must be an object with string content")
    options = payload.get("options")
    if options is not None and not isinstance(options, dict):
        raise HTTPException(400, "options must be an object")
    return model, messages, options


@router.post("/run", dependencies=[Depends(rate_limit_dangerous)])
async def run_chat(payload: dict, user: dict = Depends(require_user)):
    model, messages, _ = _validate_payload(payload)
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


# --------------------------------------------------------------------------- #
# SSE streaming
# --------------------------------------------------------------------------- #
_SSE_KEEPALIVE_S = 15.0  # comment ping while Ollama is thinking (prompt eval)


async def _chat_sse_gen(model: str, messages: list[dict], options: dict | None, user_name: str):
    """Bridge Ollama NDJSON chat chunks into SSE events.

    event: delta   data={"content": "..."}       — incremental text
    event: done    data={...final counters...}   — end of generation
    event: error   data={"error": "..."}         — stream-level failure
    """
    client = get_client()
    cancel_event = asyncio.Event()
    queue: asyncio.Queue = asyncio.Queue()
    sentinel = object()
    first_item = asyncio.Event()

    async def consume():
        try:
            async for evt in client.chat_stream(model, messages, options, cancel_event):
                queue.put_nowait(evt)
                first_item.set()
        except asyncio.CancelledError:
            raise
        except OllamaError as exc:
            queue.put_nowait(exc)
        except Exception as exc:  # pragma: no cover
            logger.warning("chat stream consumer failed: %s", exc)
            queue.put_nowait(OllamaError(f"stream failed: {exc}", code=502))
        finally:
            queue.put_nowait(sentinel)

    task = asyncio.create_task(consume())
    audited = False
    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=_SSE_KEEPALIVE_S)
            except asyncio.TimeoutError:
                if task.done():
                    continue
                yield ": keepalive\n\n"
                continue
            if item is sentinel:
                break
            if isinstance(item, OllamaError):
                await get_audit().log(user_name, "chat", model, result="error", error=item.message)
                audited = True
                yield f"event: error\ndata: {json.dumps({'error': item.message})}\n\n"
                break
            if item.get("done"):
                yield "event: done\ndata: " + json.dumps({
                    "model": item.get("model", model),
                    "total_duration": item.get("total_duration"),
                    "prompt_eval_count": item.get("prompt_eval_count"),
                    "eval_count": item.get("eval_count"),
                }) + "\n\n"
                await get_audit().log(user_name, "chat", model)
                audited = True
                break
            content = (item.get("message") or {}).get("content", "")
            if content:
                yield f"event: delta\ndata: {json.dumps({'content': content})}\n\n"
    finally:
        if not audited:
            # client disconnected mid-stream (generator closed) — audit it
            await get_audit().log(user_name, "chat", model)
        if not task.done():
            cancel_event.set()
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                task.cancel()
        else:
            task.result()  # surface unexpected consumer crash in tests


@router.post("/stream", dependencies=[Depends(rate_limit_dangerous)])
async def stream_chat(payload: dict, user: dict = Depends(require_user)):
    model, messages, options = _validate_payload(payload)
    return StreamingResponse(
        _chat_sse_gen(model, messages, options, user["username"]),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
