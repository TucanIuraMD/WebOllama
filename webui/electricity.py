"""Electricity v1.1 — host power telemetry + GPU energy integration.

Scope (LOCAL HOST ONLY — the machine running WebOllama; no SSH, no remote
agents, no sudo, no external commands per sample):

  Host-level measured sources (headline `watts`; probed in order):
    1. RAPL  — /sys/class/powercap/intel-rapl*/energy_uj counters (Intel/AMD
               CPUs). Power = ΔJ / Δt between consecutive samples. Root-only
               (0400) on many kernels/containers — unavailable is a
               FIRST-CLASS outcome, never an error.
    2. hwmon — /sys/class/hwmon/hwmon*/power*_input (µW) platform sensors.

  GPU energy (separate, GPU-ONLY — never total server power):
    3. NVML  — 0.5 s power_draw samples held in a RAM buffer by
               GpuEnergySampler (webui/gpu_energy.py); every 10 s ONE
               aggregated point (average W, energy Wh, measured interval)
               lands in the existing metrics table (kind="gpu_energy").
               No 0.5 s DB writes, no disk I/O at sampling cadence.

  HARD RULES:
    - GPU power/energy is never presented as total server power. If no
      host-level source exists, `available=false` + honest reason and the
      GPU block stands alone, clearly labeled.
    - kWh/Wh values are CALCULATED from measured power over really measured
      intervals. Gaps (sampler stopped, NVML unavailable) contribute
      NOTHING — never zero-filled, never interpolated, never double-counted
      across restarts (each DB row carries its own interval; rows of one
      process are disjoint; a restart starts a fresh buffer).
    - Cost = energy × tariff ONLY with an explicitly configured tariff —
      never an invented default.

  Storage reuses the existing `metrics` table + prune_old_metrics — no
  schema change, no second telemetry mechanism.
"""
import logging
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

PROBE_INTERVAL = 60.0  # seconds between sysfs source re-probes


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------
class RaplSource:
    """Intel/AMD RAPL package energy counters via powercap sysfs.

    energy_uj is a monotonically increasing µJ counter (wraps rarely); power
    is derived by the service from consecutive samples (ΔJ / Δt), so probing
    costs one tiny file read per package.
    """

    def __init__(self, root: str = "/sys/class/powercap") -> None:
        self.root = Path(root)
        self.domains: list[dict] = []  # [{"name", "path"}]
        self._last: Optional[dict] = None  # {"ts", "joules"}

    def probe(self) -> None:
        self.domains = []
        try:
            for top in sorted(self.root.glob("intel-rapl:*")):
                name = ""
                try:
                    name = (top / "name").read_text().strip()
                except OSError:
                    pass
                self.domains.append({"name": name or top.name, "path": top / "energy_uj"})
        except OSError:
            self.domains = []
        self._last = None

    @property
    def available(self) -> bool:
        return bool(self.domains)

    def sample(self) -> Optional[dict]:
        """Return {"watts", "joules"} for this instant, or None.

        watts is None on the first sample after (re)probing — a ΔJ/Δt needs
        two points; the caller surfaces that honestly instead of guessing.
        """
        total_uj = 0
        got = False
        for d in self.domains:
            try:
                total_uj += int(Path(d["path"]).read_text().strip())
                got = True
            except (OSError, ValueError):
                continue  # one unreadable domain must not kill the rest
        if not got:
            return None
        joules = total_uj / 1e6
        now = time.time()
        watts = None
        if self._last is not None:
            dt = now - self._last["ts"]
            dj = joules - self._last["joules"]
            if 0 < dt < 60 and 0 <= dj < 1e6:  # counter reset/wrap guard
                watts = dj / dt
        self._last = {"ts": now, "joules": joules}
        return {"watts": watts, "joules": joules}


class HwmonPowerSource:
    """Platform power sensors via standard hwmon power*_input (µW) files."""

    def __init__(self, root: str = "/sys/class/hwmon") -> None:
        self.root = Path(root)
        self.inputs: list[Path] = []
        self._probed = False

    def probe(self) -> None:
        self.inputs = []
        try:
            for hw in sorted(self.root.iterdir()):
                if not hw.is_dir():
                    continue
                try:
                    name = (hw / "name").read_text().strip().lower()
                except OSError:
                    name = ""
                if name.startswith("drivetemp"):
                    continue  # drive-level sensors are not system power
                for f in sorted(hw.glob("power*_input")):
                    self.inputs.append(f)
        except OSError:
            self.inputs = []
        self._probed = True

    @property
    def available(self) -> bool:
        if not self._probed:
            self.probe()
        return bool(self.inputs)

    def sample(self) -> Optional[dict]:
        total_uw = 0
        got = False
        for f in self.inputs:
            try:
                total_uw += int(f.read_text().strip())
                got = True
            except (OSError, ValueError):
                continue
        return {"watts": total_uw / 1e6} if got else None


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------
class ElectricityService:
    """Host-level power sample + GPU energy summaries + tariff config."""

    def __init__(self, db,
                 gpu_collector=None,
                 rapl: Optional[RaplSource] = None,
                 hwmon: Optional[HwmonPowerSource] = None,
                 gpu_energy=None) -> None:
        self.db = db
        self._gpu = gpu_collector
        self.rapl = rapl or RaplSource()
        self.hwmon = hwmon or HwmonPowerSource()
        self._gpu_energy = gpu_energy
        self._last_probe = 0.0

    # ---- collaborators ------------------------------------------------------
    @property
    def gpu(self):
        if self._gpu is not None:
            return self._gpu
        from .gpu_collector import get_gpu_collector
        return get_gpu_collector()

    @property
    def gpu_energy(self):
        if self._gpu_energy is not None:
            return self._gpu_energy
        from .gpu_energy import get_gpu_energy_sampler
        return get_gpu_energy_sampler()

    # ---- probing ------------------------------------------------------------
    def _probe(self) -> None:
        """(Re-)discover sysfs sources at most every PROBE_INTERVAL."""
        now = time.monotonic()
        if now - self._last_probe < PROBE_INTERVAL:
            return
        self._last_probe = now
        self.rapl.probe()
        self.hwmon.probe()

    # ---- main entry ----------------------------------------------------------
    async def sample(self) -> dict:
        self._probe()
        ts = time.time()

        # 1) RAPL (host-level, measured; watts known from the 2nd sample on)
        cpu_section = {"watts": None, "measured": False, "source": None}
        if self.rapl.available:
            r = self.rapl.sample()
            if r and r.get("watts") is not None:
                cpu_section = {"watts": round(r["watts"], 2), "measured": True,
                               "source": "rapl"}
            else:
                cpu_section = {"watts": None, "measured": False,
                               "source": "rapl (first sample — power next poll)"}

        # 2) hwmon platform power (host-level, measured)
        platform_section = {"watts": None, "measured": False, "source": None}
        if self.hwmon.available:
            h = self.hwmon.sample()
            if h:
                platform_section = {"watts": round(h["watts"], 2), "measured": True,
                                    "source": "hwmon"}

        # 3) NVML GPU power via the shared collector — CURRENT value only,
        #    REFERENCE/summary role, never the headline total.
        gpu_section = {"current_watts": None, "measured": False, "per_device": [],
                       "note": "GPU power draw only — NOT total server power"}
        try:
            g = await self.gpu.sample()
            rows = []
            for i, dev in enumerate((g or {}).get("gpus") or []):
                w = dev.get("power_draw")
                rows.append({"index": dev.get("index", i), "name": dev.get("name"),
                             "watts": w, "measured": w is not None})
            valid = [r["watts"] for r in rows if r["watts"] is not None]
            gpu_section.update({
                "current_watts": round(sum(valid), 2) if valid else None,
                "measured": bool(valid),
                "per_device": rows,
            })
        except Exception as exc:  # GPU collector must never break electricity
            logger.debug("electricity: GPU source failed: %s", exc)

        # headline total: ONLY host-level sources (RAPL / hwmon) qualify.
        host_sections = [cpu_section, platform_section]
        host_measured = [s for s in host_sections if s["measured"] and s["watts"] is not None]
        host_watts = round(sum(s["watts"] for s in host_measured), 2)
        host_sources = "+".join(s["source"] for s in host_measured)

        if host_measured:
            available, reason = True, None
            source = host_sources
            total = host_watts
        else:
            available, source, total = False, None, None
            reason = ("no host-level power telemetry on this machine "
                      "(RAPL unreadable/denied, no hwmon power sensors) — "
                      "total server power is UNAVAILABLE; "
                      "GPU power/energy (if any) is shown separately and is NOT a total")

        return {
            "available": available,
            "measured": available,  # headline is measured or it does not exist
            "source": source,
            "watts": total,
            "gpu_power": gpu_section,
            "components": {"cpu": cpu_section, "platform": platform_section},
            "reason": reason,
            "ts": ts,
        }

    # ---- persistence (host series for kWh summaries) ---------------------------
    async def record(self) -> None:
        """Persist one host-level power sample (metrics kind="electricity").

        Called from the 5 s metrics loop. ONLY host-level watts are written
        here; GPU 0.5 s samples never touch the DB (see GpuEnergySampler —
        they live in RAM and land as 10 s aggregated gpu_energy rows).
        """
        s = await self.sample()
        if s["available"] and s["watts"] is not None:
            await self.db.insert_metric(s["ts"], "electricity", {
                "watts": s["watts"],
                "measured": True,
                "source": s["source"],
            })

    # ---- GPU energy summaries (aggregated rows, GPU-only) ----------------------
    async def gpu_energy_windows(self) -> dict:
        """GPU energy for today / 24h / 30d + tariff + sampler liveness.

        Windows are independent integrations over aggregated gpu_energy rows;
        "today" additionally clips to local midnight, so it can never span
        more than the retention window. All values are calculated from
        measured power over really measured intervals — gaps add nothing.
        """
        def _out(s):
            return {k: s[k] for k in ("available", "energy_wh", "kwh",
                                      "average_power_w", "measured_seconds",
                                      "points", "gpus", "note")}

        midnight = time.time() - (time.time() % 86400)  # UTC day boundary
        today = _out(await self.gpu_energy.gpu_energy_summary(minutes=1440, since=midnight))
        d24 = _out(await self.gpu_energy.gpu_energy_summary(minutes=1440))
        d30 = _out(await self.gpu_energy.gpu_energy_summary(minutes=43200))
        s = self.gpu_energy
        out = {
            "today": today,
            "h24": d24,
            "month": d30,
            "sampler": {"running": s.running,
                        "last_error": s.last_error,
                        "sample_interval": s.sample_interval,
                        "aggregate_interval": s.aggregate_interval},
        }
        await self._apply_tariff_block(out, current_watts=await self._gpu_current_watts())
        return out

    # ---- tariff + cost block (v1.2) ---------------------------------------------
    async def _gpu_current_watts(self) -> Optional[float]:
        """GPU-only current power from the shared collector (never host total)."""
        try:
            g = await self.gpu.sample()
        except Exception:
            return None
        valid = [dev.get("power_draw") for dev in (g or {}).get("gpus") or []
                 if dev.get("power_draw") is not None]
        return round(sum(valid), 2) if valid else None

    async def _apply_tariff_block(self, out: dict,
                                  current_watts: Optional[float]) -> None:
        """Fill `out` with a tariff/cost block shared by GPU windows.

        Distinction enforced here (and surfaced verbatim in the UI):
          - current_cost_per_hour / average_cost_per_hour are PROJECTIONS
            ("if the current/average power is sustained for one hour");
          - cost_today / cost_24h / cost_30d are ACCUMULATED amounts for
            already-measured energy (energy × tariff).
        With no configured tariff nothing becomes 0 — every cost field is
        None plus an explicit `cost_unavailable` reason.
        """
        cfg = await self.get_config()
        tariff = cfg["tariff"] if cfg.get("tariff_is_configured") else None
        block = {
            "tariff": tariff,
            "currency": cfg.get("currency", "lei"),
            "tariff_is_configured": cfg.get("tariff_is_configured", False),
            "tariff_notice": None if cfg.get("tariff_is_configured") else
                             "tariff not configured — set lei/kWh to see cost; "
                             "no default is invented",
            # stable contract: projection keys ALWAYS exist; None = unavailable
            "current_power_w": None,
            "current_cost_per_hour": None,
            "average_power_w": None,
            "average_cost_per_hour": None,
            "cost_unavailable": None if cfg.get("tariff_is_configured")
                                else "tariff not configured",
        }
        if tariff is not None:
            # projections: power [W] × tariff [lei/kWh] / 1000 → lei per hour
            if current_watts is not None:
                block["current_power_w"] = current_watts
                block["current_cost_per_hour"] = round(current_watts * tariff / 1000.0, 4)
            if isinstance(out.get("h24"), dict) and out["h24"].get("average_power_w") is not None:
                avg = out["h24"]["average_power_w"]
                block["average_power_w"] = avg
                block["average_cost_per_hour"] = round(avg * tariff / 1000.0, 4)
            # accumulated: measured energy × tariff (energy already in kWh)
            for key in ("today", "h24", "month"):
                kwh = out[key].get("kwh") if isinstance(out.get(key), dict) else None
                if kwh is not None:
                    out[key]["cost"] = round(kwh * tariff, 4)
                    out[key]["currency"] = cfg.get("currency", "lei")
                    out[key]["cost_note"] = "calculated: measured energy × tariff"
        else:
            for key in ("today", "h24", "month"):
                if isinstance(out.get(key), dict):
                    out[key]["cost"] = None
                    out[key]["cost_unavailable"] = "tariff not configured"
        out["tariff"] = block

    async def apply_tariff_to_window(self, out: dict) -> None:
        """Attach cost to an arbitrary GPU window (selected period card)."""
        cfg = await self.get_config()
        if cfg.get("tariff_is_configured"):
            if out.get("kwh") is not None:
                out["cost"] = round(out["kwh"] * cfg["tariff"], 4)
                out["currency"] = cfg.get("currency", "lei")
                out["cost_note"] = "calculated: measured energy × tariff"
        else:
            out["cost"] = None
            out["cost_unavailable"] = "tariff not configured"

    # ---- host energy summary (host-level series, not GPU) ----------------------
    async def energy_summary_host(self, minutes: int = 60) -> dict:
        """Host kWh via trapezoidal integration of the persisted watts series.

        `measured` is true only if EVERY integrated point was measured.
        Gaps larger than 600s (restarts, retention pruning) are skipped, so
        intervals are never double-counted across an app restart.
        """
        since = time.time() - minutes * 60
        rows = await self.db.get_metrics("electricity", since)
        pts = [(r["ts"], r["data"]) for r in rows
               if isinstance(r.get("data"), dict) and r["data"].get("watts") is not None]
        pts.sort(key=lambda x: x[0])
        watt_hours = 0.0
        all_measured = bool(pts)
        integrated = 0
        for (t0, d0), (t1, d1) in zip(pts, pts[1:]):
            dt = t1 - t0
            if dt <= 0 or dt > 600:  # huge gap (restart/prune) — never integrate
                continue
            watt_hours += (d0["watts"] + d1["watts"]) / 2.0 * (dt / 3600.0)
            integrated += 1
            if not (d0.get("measured") and d1.get("measured")):
                all_measured = False
        window = (pts[-1][0] - pts[0][0]) if len(pts) > 1 else 0.0
        kwh = round(watt_hours / 1000.0, 6) if integrated else None
        return {
            "kwh": kwh,
            "energy_basis": "calculated-from-measured-power" if (kwh is not None and all_measured)
                            else ("calculated-from-partially-unavailable-power" if kwh is not None
                                  else None),
            "points": len(pts),
            "integrated_intervals": integrated,
            "window_seconds": round(window, 1),
            "measured": all_measured if integrated else False,
            "method": "trapezoid-integration",
        }

    # ---- tariff config ---------------------------------------------------------
    async def get_config(self) -> dict:
        tariff = await self.db.get_setting("electricity_tariff", "")
        currency = await self.db.get_setting("electricity_currency", "lei")
        try:
            tariff_f = float(tariff)
        except (TypeError, ValueError):
            tariff_f = None
        configured = tariff_f is not None and tariff_f > 0
        return {"tariff": tariff_f, "currency": currency,
                "tariff_is_configured": configured,
                "notice": None if configured else
                          "tariff not configured — set lei/kWh to see cost; "
                          "no default is invented"}

    async def set_config(self, tariff: Optional[float] = None,
                         currency: Optional[str] = None) -> dict:
        if tariff is not None:
            if not (0 <= float(tariff) <= 1000):
                raise ValueError("tariff must be within [0, 1000]")
            await self.db.set_setting("electricity_tariff", repr(float(tariff)))
        if currency is not None:
            cur = str(currency).strip()
            if len(cur) > 8 or not cur:
                raise ValueError("currency must be a short symbol (<= 8 chars)")
            await self.db.set_setting("electricity_currency", cur)
        return await self.get_config()


_service: Optional[ElectricityService] = None


def get_electricity_service() -> ElectricityService:
    global _service
    if _service is None:
        from .db import Database
        _service = ElectricityService(Database.get())
    return _service


def set_electricity_service(svc: Optional[ElectricityService]) -> None:
    global _service
    _service = svc
