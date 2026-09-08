"""Electricity v1 — host power telemetry + energy integration.

Scope (v1, LOCAL HOST ONLY — the machine running WebOllama; no SSH, no
remote agents, no sudo, no external commands per sample):

  Measured sources (probed in order, first host-level source wins):
    1. RAPL  — /sys/class/powercap/intel-rapl*/energy_uj counters (Intel/AMD
               CPUs). Power = ΔJ / Δt between consecutive samples. Note: on
               many production kernels (and inside most containers/VMs) these
               files are root-only (mode 0400) — unavailable is a FIRST-CLASS
               outcome, never an error.
    2. hwmon — /sys/class/hwmon/hwmon*/power*_input (µW) platform sensors.
    3. NVML  — per-GPU power_draw via the EXISTING GPUCollector (reuses the
               same cached NVML handle the GPU page uses; no new polling).

  HARD RULE: GPU power is NEVER presented as total server power. The
  headline `watts` is non-None only when at least one HOST-level source
  (RAPL or hwmon) is readable. GPU-only hosts report
  available=false + a clearly-labeled GPU-only reference value.

  Integration: kWh over the retained metrics window via trapezoidal
  integration of the persisted watts series. `measured` is true only when
  every integrated point came from a measured host source.

  Cost: kWh × tariff (settings), shown ONLY when the tariff is explicitly
  configured — never an invented default.

Design constraints honored:
  - Light polling: tiny sysfs reads; source re-probe at most every
    PROBE_INTERVAL seconds; NVML sampling rides the GPU collector's cache.
  - Storage reuses the existing `metrics` table (kind="electricity") and
    the existing prune_old_metrics retention — no schema change, no second
    telemetry mechanism.
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
    """Aggregates power sources into one sample; persists + integrates kWh."""

    def __init__(self, db,
                 gpu_collector=None,
                 rapl: Optional[RaplSource] = None,
                 hwmon: Optional[HwmonPowerSource] = None) -> None:
        self.db = db
        self._gpu = gpu_collector
        self.rapl = rapl or RaplSource()
        self.hwmon = hwmon or HwmonPowerSource()
        self._last_probe = 0.0

    # ---- collaborators ------------------------------------------------------
    @property
    def gpu(self):
        if self._gpu is not None:
            return self._gpu
        from .gpu_collector import get_gpu_collector
        return get_gpu_collector()

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

        # 3) NVML GPU power via the shared collector — REFERENCE ONLY, never
        #    the headline total by itself.
        gpu_section = {"watts": None, "measured": False, "per_device": [],
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
                "watts": round(sum(valid), 2) if valid else None,
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
                      "GPU power draw (if any) is shown separately and is NOT a total")

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

    # ---- persistence + integration -------------------------------------------
    async def record(self) -> None:
        """Persist one compact sample into metrics(kind='electricity')."""
        s = await self.sample()
        if s["available"]:
            await self.db.insert_metric(s["ts"], "electricity", {
                "watts": s["watts"],
                "gpu_watts": s["gpu_power"].get("watts"),
                "measured": s["measured"],
                "source": s["source"],
            })

    async def energy_summary(self, minutes: int = 60) -> dict:
        """Trapezoidal kWh integration over the retained watts series.

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
