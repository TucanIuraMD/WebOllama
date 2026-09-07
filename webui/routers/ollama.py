"""Model management endpoints — list, show, copy, delete, stop, pull, push, create, running."""
import logging

from fastapi import APIRouter, Depends, HTTPException

from ..audit import get_audit
from ..deps import rate_limit_dangerous, require_user
from ..jobs import JobManager, get_job_manager
from ..ollama_client import OllamaClient, OllamaError, get_client

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/ollama", tags=["models"])


def _client() -> OllamaClient:
    return get_client()


def _jm() -> JobManager:
    return get_job_manager()


# ---- Long-running operations (submitted as jobs) — declared BEFORE wildcard routes ----
@router.post("/models/pull", dependencies=[Depends(rate_limit_dangerous)])
async def pull_model(payload: dict, user: dict = Depends(require_user)):
    name = str(payload.get("name", "")).strip()
    if not name:
        raise HTTPException(400, "model name required")
    job = await _jm().submit("pull", name, lambda j: _jm().run_pull(j, name))
    await get_audit().log(user["username"], "pull", name)
    return {"job_id": job.id, "model": name, "status": "running"}


@router.post("/models/push", dependencies=[Depends(rate_limit_dangerous)])
async def push_model(payload: dict, user: dict = Depends(require_user)):
    name = str(payload.get("name", "")).strip()
    if not name:
        raise HTTPException(400, "model name required")
    job = await _jm().submit("push", name, lambda j: _jm().run_push(j, name))
    await get_audit().log(user["username"], "push", name)
    return {"job_id": job.id, "model": name, "status": "running"}


@router.post("/models/create", dependencies=[Depends(rate_limit_dangerous)])
async def create_model(payload: dict, user: dict = Depends(require_user)):
    name = str(payload.get("name", "")).strip()
    modelfile = str(payload.get("modelfile", "")).strip()
    if not name:
        raise HTTPException(400, "model name required")
    if not modelfile:
        raise HTTPException(400, "modelfile required")
    job = await _jm().submit("create", name, lambda j: _jm().run_create(j, name, modelfile))
    await get_audit().log(user["username"], "create", name)
    return {"job_id": job.id, "model": name, "status": "running"}


@router.post("/models/batch-delete", dependencies=[Depends(rate_limit_dangerous)])
async def batch_delete_models(payload: dict, user: dict = Depends(require_user)):
    names = payload.get("models", [])
    if not isinstance(names, list) or not names:
        raise HTTPException(400, "models list required")
    if len(names) > 50:
        raise HTTPException(400, "max 50 models per batch")
    results = []
    for name in names:
        try:
            await _client().delete(name)
            await get_audit().log(user["username"], "delete", name)
            results.append({"model": name, "ok": True})
        except Exception as exc:
            results.append({"model": name, "ok": False, "error": str(exc)})
    return {"results": results}


@router.get("/running")
async def running_models(user: dict = Depends(require_user)):
    try:
        models = await _client().running_models()
        return {"models": models, "count": len(models)}
    except OllamaError as exc:
        raise HTTPException(502, detail=exc.message)


@router.get("/models")
async def list_models(user: dict = Depends(require_user)):
    try:
        models = await _client().tags()
        # Running state comes from the same client (/api/ps) — one source of
        # truth, no separate model-state store. If Ollama is offline for the
        # ps call the list is still served, just without running flags.
        try:
            running_names = {m.get("name") for m in await _client().running_models()}
        except OllamaError:
            running_names = set()
        for m in models:
            m["running"] = m.get("name") in running_names
        return {"models": models, "count": len(models)}
    except OllamaError as exc:
        raise HTTPException(status_code=502, detail=exc.message)


@router.post("/models/{name:path}/run")
async def run_model(
    name: str,
    user: dict = Depends(require_user),
    rate=Depends(rate_limit_dangerous),
):
    """Load a model into VRAM (Ollama: empty-prompt generate + keep_alive)."""
    try:
        await _client().load(name)
        await get_audit().log(user["username"], "run", name)
        return {"ok": True, "model": name, "running": True}
    except OllamaError as exc:
        raise HTTPException(502, detail=exc.message)


# ---- specific actions on a model (declared before the {name:path} catch-all) ----------
@router.post("/models/{name:path}/show")
async def show_model(name: str, user: dict = Depends(require_user)):
    try:
        data = await _client().show(name)
        # Real Ollama puts context_length in model_info ("{family}.context_length"),
        # not in details — surface it uniformly for the UI.
        if data.get("context_length") is None:
            info = data.get("model_info") or {}
            for k, v in info.items():
                if k.endswith(".context_length"):
                    data["context_length"] = v
                    break
        return data
    except OllamaError as exc:
        raise HTTPException(502, detail=exc.message)


@router.post("/models/{name:path}/copy")
async def copy_model(
    name: str,
    payload: dict,
    user: dict = Depends(require_user),
    rate=Depends(rate_limit_dangerous),
):
    destination = str(payload.get("destination", "")).strip()
    if not destination:
        raise HTTPException(400, "destination required")
    try:
        await _client().copy(name, destination)
        await get_audit().log(user["username"], "copy", f"{name} -> {destination}")
        return {"ok": True, "source": name, "destination": destination}
    except OllamaError as exc:
        raise HTTPException(502, detail=exc.message)


@router.delete("/models/{name:path}/stop")
async def stop_model(
    name: str,
    user: dict = Depends(require_user),
    rate=Depends(rate_limit_dangerous),
):
    try:
        await _client().stop(name)
        await get_audit().log(user["username"], "stop", name)
        return {"ok": True, "model": name}
    except OllamaError as exc:
        raise HTTPException(502, detail=exc.message)


@router.get("/models/{name:path}")
async def get_model(name: str, user: dict = Depends(require_user)):
    try:
        models = await _client().tags()
        for m in models:
            if m["name"] == name:
                return m
        raise HTTPException(404, f"model {name!r} not found")
    except OllamaError as exc:
        raise HTTPException(502, detail=exc.message)


@router.delete("/models/{name:path}")
async def delete_model(
    name: str,
    user: dict = Depends(require_user),
    rate=Depends(rate_limit_dangerous),
):
    try:
        await _client().delete(name)
        await get_audit().log(user["username"], "delete", name)
        return {"ok": True, "model": name}
    except OllamaError as exc:
        raise HTTPException(502, detail=exc.message)