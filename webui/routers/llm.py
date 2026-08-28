"""LLM API endpoints — OpenAI-compatible endpoint management."""
import logging

from fastapi import APIRouter, Depends, HTTPException

from ..audit import get_audit
from ..deps import current_user, rate_limit_dangerous, require_user
from ..llm_api import LLMError, get_llm_manager, validate_endpoint_url

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/llm", tags=["llm"])


def _mgr():
    return get_llm_manager()


@router.get("")
async def list_endpoints(user: dict = Depends(require_user)):
    return {"endpoints": await _mgr().snapshot()}


@router.get("/{eid}")
async def get_endpoint(eid: str, user: dict = Depends(require_user)):
    cfg = await _mgr().get_endpoint(eid)
    if cfg is None:
        raise HTTPException(404, "endpoint not found")
    return _mgr().public_endpoint(cfg)


@router.get("/{eid}/models")
async def get_models(eid: str, user: dict = Depends(require_user)):
    try:
        return await _mgr().models(eid)
    except LLMError as exc:
        raise HTTPException(404, str(exc))


@router.post("/{eid}/check", dependencies=[Depends(rate_limit_dangerous)])
async def check_endpoint(eid: str, user: dict = Depends(require_user)):
    try:
        status = await _mgr().check_one(eid)
        return status
    except LLMError as exc:
        raise HTTPException(404, str(exc))


@router.post("", dependencies=[Depends(rate_limit_dangerous)])
async def add_endpoint(payload: dict, user: dict = Depends(current_user)):
    if user["role"] != "admin":
        raise HTTPException(403, "admin required")
    eps = await _mgr().load_endpoints()
    eid = str(payload.get("id", "")).strip().lower()
    name = str(payload.get("name", "")).strip()
    try:
        base_url = validate_endpoint_url(str(payload.get("base_url", "")))
    except LLMError as exc:
        raise HTTPException(400, str(exc))
    api_key = str(payload.get("api_key", "")).strip()
    enabled = bool(payload.get("enabled", True))

    if not eid or not name:
        raise HTTPException(400, "id and name required")
    if not re_fullmatch_id(eid):
        raise HTTPException(400, "id must be [a-z0-9_-]")
    if any(e.get("id") == eid for e in eps):
        raise HTTPException(400, "endpoint id already exists")

    eps.append({
        "id": eid,
        "name": name,
        "type": "openai-compatible",
        "base_url": base_url,
        "api_key": api_key,
        "enabled": enabled,
    })
    await _mgr().save_endpoints(eps)
    await get_audit().log(user["username"], "add_llm_endpoint", eid)
    return {"ok": True, "endpoint": _mgr().public_endpoint(eps[-1])}


@router.put("/{eid}", dependencies=[Depends(rate_limit_dangerous)])
async def update_endpoint(eid: str, payload: dict, user: dict = Depends(current_user)):
    if user["role"] != "admin":
        raise HTTPException(403, "admin required")
    eps = await _mgr().load_endpoints()
    idx = next((i for i, e in enumerate(eps) if e.get("id") == eid), None)
    if idx is None:
        raise HTTPException(404, "endpoint not found")
    cur = eps[idx]
    if "name" in payload and str(payload["name"]).strip():
        cur["name"] = str(payload["name"]).strip()
    if "base_url" in payload and str(payload["base_url"]).strip():
        try:
            cur["base_url"] = validate_endpoint_url(str(payload["base_url"]))
        except LLMError as exc:
            raise HTTPException(400, str(exc))
    if "api_key" in payload:
        # keep existing key if the client sends an empty/masked placeholder
        new_key = str(payload["api_key"]).strip()
        if new_key and not new_key.endswith("****"):
            cur["api_key"] = new_key
    if "enabled" in payload:
        cur["enabled"] = bool(payload["enabled"])
    eps[idx] = cur
    await _mgr().save_endpoints(eps)
    await get_audit().log(user["username"], "update_llm_endpoint", eid)
    return {"ok": True, "endpoint": _mgr().public_endpoint(cur)}


@router.delete("/{eid}", dependencies=[Depends(rate_limit_dangerous)])
async def delete_endpoint(eid: str, user: dict = Depends(current_user)):
    if user["role"] != "admin":
        raise HTTPException(403, "admin required")
    eps = await _mgr().load_endpoints()
    if not any(e.get("id") == eid for e in eps):
        raise HTTPException(404, "endpoint not found")
    eps = [e for e in eps if e.get("id") != eid]
    await _mgr().save_endpoints(eps)
    await get_audit().log(user["username"], "delete_llm_endpoint", eid)
    return {"ok": True}


def re_fullmatch_id(value: str) -> bool:
    import re
    return bool(re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", value))
