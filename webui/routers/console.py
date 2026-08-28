"""Ollama Console (Terminal page) — whitelisted commands only."""
import logging

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect

from ..audit import get_audit
from ..deps import rate_limit_dangerous, require_user
from ..ollama_console import ConsoleError, OllamaConsole, parse_command
from ..ws_manager import get_ws_manager

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/console", tags=["console"])


@router.get("/allowed")
async def allowed_commands(user: dict = Depends(require_user)):
    from ..ollama_console import ALLOWED_COMMANDS

    return {"commands": ALLOWED_COMMANDS}


@router.post("/run", dependencies=[Depends(rate_limit_dangerous)])
async def run_command(payload: dict, user: dict = Depends(require_user)):
    text = str(payload.get("command", "")).strip()
    if not text:
        raise HTTPException(400, "command required")
    console = _get_console()
    result = await console.run(text)
    if result.error:
        await get_audit().log(user["username"], "console", text, "error", result.error)
    else:
        await get_audit().log(user["username"], "console", text)
    return {
        "command": result.command,
        "output": result.output,
        "error": result.error,
        "exit_code": result.exit_code,
        "duration": result.duration,
        "api": result.api,
        "job_id": result.job_id,
    }


_console: OllamaConsole | None = None


def _get_console() -> OllamaConsole:
    global _console
    if _console is None:
        from ..ollama_client import get_client

        _console = OllamaConsole(get_client())
    return _console


def set_console_job_hook(hook) -> None:
    console = _get_console()
    console.submit_job = hook