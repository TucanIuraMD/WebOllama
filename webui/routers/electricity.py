"""Electricity v1.1 endpoints — host power + GPU energy history.

GET  /api/electricity            — live host sample (never 500: structured
                                   {"available": false, ...}) + GPU current W.
GET  /api/electricity/gpu-energy — aggregated GPU energy: today / 24h / 30d
                                   (+ per-GPU split, sampler state, gaps
                                   reflected as measured_seconds only).
GET  /api/electricity/gpu-history — aggregated gpu_energy points (chart feed).
GET  /api/electricity/summary    — host kWh over a window (from the 5s host
                                   power series) + optional cost.
GET  /api/electricity/config     — tariff + currency (admin).
PUT  /api/electricity/config     — set tariff/currency (admin, rate-limited).

Local host only by design: no SSH, no remote agents. No sudo: all sources
are world-readable sysfs / the existing NVML handle. GPU values are always
GPU-only and are never presented as total server power.
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
    """Current host power draw + GPU current draw (reference only)."""
    return await get_electricity_service().sample()


@router.get("/gpu-energy")
async def gpu_energy(user: dict = Depends(current_user)):
    """Aggregated GPU energy: today / 24h / 30d + tariff + sampler state.

    The tariff block separates PROJECTIONS (current_cost_per_hour,
    average_cost_per_hour) from ACCUMULATED amounts (cost today/24h/30d =
    measured energy × tariff). GPU-only — never total server.
    """
    return await get_electricity_service().gpu_energy_windows()


@router.get("/gpu-energy-window")
async def gpu_energy_window(
    minutes: int = Query(default=60, ge=1, le=1440),
    user: dict = Depends(current_user),
):
    """Aggregated GPU energy for an arbitrary window (selected period)."""
    svc = get_electricity_service()
    s = await svc.gpu_energy.gpu_energy_summary(minutes=minutes)
    out = dict(s)
    out["minutes"] = minutes
    out["window_label"] = _period_label(minutes)
    await svc.apply_tariff_to_window(out)
    return out


def _period_label(minutes: int) -> str:
    if minutes % 43200 == 0:
        return f"{minutes // 43200} mo"
    if minutes % 1440 == 0:
        return f"{minutes // 1440} d"
    if minutes % 60 == 0:
        return f"{minutes // 60} h"
    return f"{minutes} m"


@router.get("/gpu-history")
async def gpu_energy_history(
    minutes: int = Query(default=60, ge=1, le=1440),
    user: dict = Depends(current_user),
):
    """Aggregated gpu_energy points (one per ~10 s) for charts."""
    db = Database.get()
    since = time.time() - minutes * 60
    rows = await db.get_metrics("gpu_energy", since)
    return {"kind": "gpu_energy", "minutes": minutes, "points": rows,
            "retention": HISTORY_RETENTION}


@router.get("/summary")
async def electricity_summary(
    minutes: int = Query(default=60, ge=1, le=1440),
    user: dict = Depends(current_user),
):
    """Host-level kWh (and optional cost) over the requested window."""
    svc = get_electricity_service()
    summary = await svc.energy_summary_host(minutes=minutes)
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
    """Raw persisted host watts points for charts (5s cadence)."""
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
