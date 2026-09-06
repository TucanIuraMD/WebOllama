"""Agents tab — practical Models × Agents knowledge base.

Deliberately separate from any objective benchmark: this module stores
human-verified per (model, agent) assessments only. Nothing here scores,
ranks or auto-fills statuses. The model list is NOT stored locally — it is
reused live from the existing OllamaClient (GET /api/tags via /api/ollama).
"""
import logging
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from ..audit import get_audit
from ..db import Database
from ..deps import rate_limit_dangerous, require_user
from ..ollama_client import OllamaError, get_client

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/agents", tags=["agents"])


def _db() -> Database:
    return Database.get()


def _slugify(name: str) -> str:
    import re

    s = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return s


async def _resolve_capabilities(raw) -> list[int]:
    """Accept capability ids or slugs; unknown items are rejected."""
    if raw in (None, []):
        return []
    if not isinstance(raw, list):
        raise HTTPException(400, "capabilities must be a list of ids or slugs")
    known = {c["id"]: c for c in await _db().list_capabilities()}
    by_slug = {c["slug"]: c for c in known.values()}
    ids = []
    for item in raw:
        if isinstance(item, int) or (isinstance(item, str) and item.isdigit()):
            cap = known.get(int(item))
        else:
            cap = by_slug.get(str(item))
        if cap is None:
            raise HTTPException(400, f"unknown capability: {item!r}")
        ids.append(cap["id"])
    return ids


# ---- config -----------------------------------------------------------------
@router.get("")
async def list_agents(user: dict = Depends(require_user)):
    """All configured agents (rows, not hardcoded) + capabilities."""
    return {
        "agents": await _db().list_agents(),
        "capabilities": await _db().list_capabilities(),
    }


@router.get("/capabilities")
async def list_capabilities(user: dict = Depends(require_user)):
    return {"capabilities": await _db().list_capabilities()}


@router.post("/agents", dependencies=[Depends(rate_limit_dangerous)])
async def create_agent(payload: dict, user: dict = Depends(require_user)):
    """Add a new agent/environment at runtime (e.g. Automation, Documents)."""
    if user.get("role") != "admin":
        raise HTTPException(403, "admin required")
    name = str(payload.get("name", "")).strip()
    if not name:
        raise HTTPException(400, "agent name required")
    slug = str(payload.get("slug", "")).strip().lower() or _slugify(name)
    if not slug:
        raise HTTPException(400, "agent slug required")
    if await _db().get_agent_by_slug(slug):
        raise HTTPException(400, f"agent slug already exists: {slug}")
    agent = await _db().create_agent(
        name, slug, str(payload.get("description", "")).strip(),
        bool(payload.get("enabled", True)),
    )
    await get_audit().log(user["username"], "create_agent", slug)
    return {"ok": True, "agent": agent}


@router.post("/capabilities", dependencies=[Depends(rate_limit_dangerous)])
async def create_capability(payload: dict, user: dict = Depends(require_user)):
    """Add a new capability at runtime (e.g. OCR, Translation, Reasoning)."""
    if user.get("role") != "admin":
        raise HTTPException(403, "admin required")
    name = str(payload.get("name", "")).strip()
    if not name:
        raise HTTPException(400, "capability name required")
    slug = str(payload.get("slug", "")).strip().lower() or _slugify(name)
    if not slug:
        raise HTTPException(400, "capability slug required")
    if any(c["slug"] == slug for c in await _db().list_capabilities()):
        raise HTTPException(400, f"capability slug already exists: {slug}")
    cap = await _db().create_capability(
        name, slug, str(payload.get("description", "")).strip()
    )
    await get_audit().log(user["username"], "create_capability", slug)
    return {"ok": True, "capability": cap}


# ---- matrix -----------------------------------------------------------------
@router.get("/matrix")
async def get_matrix(user: dict = Depends(require_user)):
    """Models (live from Ollama) × agents with saved assessments.

    Degrades gracefully when Ollama is offline: models=[] but saved
    assessments are still returned so the knowledge base stays readable.
    """
    db = _db()
    models: list[dict] = []
    ollama_online = False
    try:
        tags = await get_client().tags()
        ollama_online = True
        for m in tags:
            d = m.get("details", {}) or {}
            models.append({
                "name": m.get("name", ""),
                "size": m.get("size"),
                "modified_at": m.get("modified_at"),
                "family": d.get("family"),
                "parameter_size": d.get("parameter_size"),
                "quantization_level": d.get("quantization_level"),
                "ollama_capabilities": m.get("capabilities") or [],
            })
    except OllamaError as exc:
        logger.warning("agents matrix: ollama unavailable: %s", exc.message)

    assessments = await db.list_assessments()
    return {
        "ollama_online": ollama_online,
        "agents": await db.list_agents(),
        "capabilities": await db.list_capabilities(),
        "models": models,
        "assessments": assessments,
        "counts": {
            "models": len(models),
            "agents": len(await db.list_agents()),
            "assessments": len(assessments),
            "tested": sum(1 for a in assessments if a["status"] != "untested"),
        },
    }


@router.get("/models/{model:path}")
async def get_model_assessments(model: str, user: dict = Depends(require_user)):
    """One model: live Ollama meta (if present) + all its assessments."""
    db = _db()
    in_ollama = False
    meta: Optional[dict] = None
    try:
        for m in await get_client().tags():
            if m.get("name") == model:
                in_ollama = True
                d = m.get("details", {}) or {}
                meta = {
                    "name": m.get("name", ""),
                    "size": m.get("size"),
                    "modified_at": m.get("modified_at"),
                    "family": d.get("family"),
                    "parameter_size": d.get("parameter_size"),
                    "quantization_level": d.get("quantization_level"),
                    "ollama_capabilities": m.get("capabilities") or [],
                }
                break
    except OllamaError:
        pass  # offline: still serve saved assessments from DB
    return {
        "model": model,
        "in_ollama": in_ollama,
        "model_meta": meta,
        "assessments": await db.list_assessments(model=model),
    }


# ---- assessments --------------------------------------------------------------
@router.post("/assessments", dependencies=[Depends(rate_limit_dangerous)])
async def save_assessment(payload: dict, user: dict = Depends(require_user)):
    """Create/update one model×agent assessment.

    Body: {model, agent_id | agent (slug), status, note?, capabilities?,
    tested_at?}. tested_at is generated server-side; untested clears it.
    """
    db = _db()
    model = str(payload.get("model", "")).strip()
    if not model:
        raise HTTPException(400, "model required")

    agent = None
    if payload.get("agent_id") is not None:
        try:
            agent = await db.get_agent(int(payload["agent_id"]))
        except (TypeError, ValueError):
            raise HTTPException(400, "agent_id must be an integer")
    elif payload.get("agent"):
        agent = await db.get_agent_by_slug(str(payload["agent"]).strip().lower())
    if agent is None:
        raise HTTPException(400, "unknown agent")

    status = str(payload.get("status", "untested")).strip().lower()
    if status not in db.VALID_ASSESSMENT_STATUSES:
        raise HTTPException(400, f"status must be one of {list(db.VALID_ASSESSMENT_STATUSES)}")

    tested_at = payload.get("tested_at")
    if tested_at is not None:
        try:
            tested_at = float(tested_at)
        except (TypeError, ValueError):
            raise HTTPException(400, "tested_at must be a unix timestamp")
    if status == "untested":
        tested_at = None  # untested is not a test result

    cap_ids = await _resolve_capabilities(payload.get("capabilities"))
    await db.upsert_assessment(
        model=model,
        agent_id=agent["id"],
        status=status,
        note=str(payload.get("note", "") or ""),
        capabilities=cap_ids,
        tested_at=tested_at,
    )
    # return the joined row (agent_name/slug + capabilities) — same shape as
    # GET /api/agents/matrix assessments, so clients can patch in place
    row = next(
        (a for a in await db.list_assessments(model=model) if a["agent_id"] == agent["id"]),
        None,
    )
    await get_audit().log(user["username"], "save_assessment", f"{model} @ {agent['slug']} -> {status}")
    return {"ok": True, "assessment": row}


@router.delete("/assessments/{assessment_id}", dependencies=[Depends(rate_limit_dangerous)])
async def delete_assessment(assessment_id: int, user: dict = Depends(require_user)):
    """Reset a cell: removing the assessment row == back to untested."""
    deleted = await _db().delete_assessment(assessment_id)
    if not deleted:
        raise HTTPException(404, "assessment not found")
    await get_audit().log(user["username"], "delete_assessment", str(assessment_id))
    return {"ok": True}
