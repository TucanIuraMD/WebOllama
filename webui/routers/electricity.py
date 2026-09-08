"""Electricity v1 endpoints — host power telemetry + energy history.

GET  /api/electricity            — one live sample (never 500: structured
                                   {"available": false, ...} when the host
                                   has no power telemetry source).
GET  /api/electricity/summary    — kWh over the retention window via
                                   trapezoidal integration of the persisted
                                   watts series + optional cost estimate.
GET  /api/electricity/history    — raw persisted watts points (chart feed).
GET  /api/electricity/config     — tariff + currency (admin).
PUT  /api/electricity/config     — set tariff/currency (admin, rate-limited).

Local host only by design: no SSH, no remote agents. No sudo: all sources
are world-readable sysfs / the existing NVML handle.
"""
import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Query

from ..config import HISTORY_RETENTION
from ..db import Database
from ..deps import current_user, rate_limit_dangerous
from ..electricity import get_electricity_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/electricity", tags=["electricity"])


@router.get("")
async def electricity(user: dict = Depends(current_user)):
    """Current power draw of the host running WebOllama.

    200 with a structured payload in every case; `available: false` carries
    an honest `reason` when the host exposes no power source.
    """
    return await get_electricity_service().sample()


@router.get("/summary")
async def electricity_summary(
    minutes: int = Query(default=60, ge=1, le=1440),
    user: dict = Depends(current_user),
):
    """kWh (and optional cost) over the requested window, from history."""
    svc = get_electricity_service()
    summary = await svc.energy_summary(minutes=minutes)
    config = await svc.get_config()
    out = {**summary, "minutes": minutes,
           "retention_seconds": HISTORY_RETENTION,
           "window_bounded_by_retention": minutes * 60 > HISTORY_RETENTION}
    if config.get("tariff_is_configured") and out.get("kwh") is not None:
        out["cost"] = round(out["kwh"] * config["tariff"], 4)
        out["currency"] = config["currency"]
    out["tariff_configured"] = bool(config.get("tariff_is_configured"))
    return out


@router.get("/history")
async def electricity_history(
    minutes: int = Query(default=60, ge=1, le=1440),
    user: dict = Depends(current_user),
):
    """Raw persisted watts points for charts."""
    db = Database.get()
    since = time.time() - minutes * 60
    rows = await db.get_metrics("electricity", since)
    return {"kind": "electricity", "minutes": minutes, "points": rows,
            "retention": HISTORY_RETENTION}


@router.get("/config")
async def get_electricity_config(user: dict = Depends(current_user)):
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin required")
    return await get_electricity_service().get_config()


@router.put("/config", dependencies=[Depends(rate_limit_dangerous)])
async def put_electricity_config(payload: dict, user: dict = Depends(current_user)):
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin required")
    tariff = payload.get("tariff")
    currency = payload.get("currency")
    if tariff is None and currency is None:
        raise HTTPException(status_code=422, detail="nothing to update")
    try:
        return await get_electricity_service().set_config(
            tariff=None if tariff is None else float(tariff),
            currency=None if currency is None else str(currency),
        )
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc))
