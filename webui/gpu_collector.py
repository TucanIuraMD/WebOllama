"""GPU data collector.

The app is designed to run ON the machine that hosts the GPU, so local
collection is authoritative. Sources in priority order:

1. NVML via pynvml (fast, precise — clocks, power, PCIe, processes)
2. nvidia-smi CLI (fallback, single subprocess call per sample — no per-second forks)
3. Remote SYSINFO_URL endpoint (optional fallback only, never required)

Fan control: the Tesla V100 fan is governed by `v100-fan.service` (ESP32
controller). The collector attaches fan_target / fan_pwm from that system
(via webui.v100_fan) and NEVER surfaces the NVML fan speed as V100 fan control.

Robustness contract:
- One failed metric NEVER breaks the whole snapshot → the field becomes None.
- NVML is primary; nvidia-smi is fallback; remote is optional.
- Persistent failures are logged at most once per WARN_INTERVAL (no per-second spam).

When no NVIDIA GPU is reachable the collector returns
{"available": false, "reason": "..."} — never fabricated values.
"""
import asyncio
import logging
import shutil
import time
from typing import Any, Optional

import httpx

from .config import SYSINFO_URL

logger = logging.getLogger(__name__)

# NVML returns this sentinel for "value not available" on c_uint fields
NVML_NOT_AVAILABLE = 0xFFFFFFFF

_GPU_QUERY = (
    "index,name,uuid,driver_version,temperature.gpu,utilization.gpu,"
    "utilization.memory,memory.total,memory.used,memory.free,power.draw,"
    "power.limit,pstate,clocks.sm,clocks.mem,clocks.max.sm,clocks.max.mem,"
    "fan.speed,pcie.link.gen.current,pcie.link.width.current,pcie.link.gen.max,"
    "pcie.link.width.max,compute_mode,persistence_mode,display_active,"
    "encoder.stats.sessionCount,decoder.stats.sessionCount,performance_state"
)
_PROCS_QUERY = "pid,process_name,used_memory,gpu_uuid"


class GPUNotAvailable(Exception):
    pass


class GPUCollector:
    WARN_INTERVAL = 30.0  # seconds between repeated failure warnings

    def __init__(self, sysinfo_url: str = SYSINFO_URL) -> None:
        self.sysinfo_url = sysinfo_url.rstrip("/")
        self._smi_path: Optional[str] = None
        self._nvml_available: Optional[bool] = None
        self._lock = asyncio.Lock()
        self._last = 0.0
        self._cache: dict = {"available": False, "reason": "collector not run yet"}
        self._cache_ttl = 0.9  # seconds
        self._last_warn = 0.0

    # ---- logging --------------------------------------------------------------
    def _warn_throttled(self, msg: str, *args: Any) -> None:
        now = time.monotonic()
        if now - self._last_warn >= self.WARN_INTERVAL:
            self._last_warn = now
            logger.warning(msg, *args)

    # ---- detection ------------------------------------------------------------
    def _find_nvidia_smi(self) -> str | None:
        if self._smi_path is not None:
            return self._smi_path or None
        import os
        for name in ("nvidia-smi", "/usr/bin/nvidia-smi", "/usr/local/bin/nvidia-smi"):
            p = shutil.which(name)
            if p and os.path.exists(p):
                self._smi_path = p
                return p
            if name.startswith("/") and os.path.exists(name):
                self._smi_path = name
                return name
        self._smi_path = ""  # mark probed (no binary)
        return None

    def _probe_nvml(self) -> bool:
        if self._nvml_available is not None:
            return self._nvml_available
        try:
            import pynvml  # type: ignore
            pynvml.nvmlInit()
            pynvml.nvmlShutdown()
            self._nvml_available = True
        except Exception:
            self._nvml_available = False
        return self._nvml_available

    # ---- sampling -------------------------------------------------------------
    async def sample(self) -> dict:
        now = time.monotonic()
        if now - self._last < self._cache_ttl:
            return self._cache
        self._last = now
        try:
            data = await self._collect()
        except GPUNotAvailable as exc:
            self._warn_throttled("GPU unavailable: %s", exc)
            data = {"available": False, "reason": str(exc), "gpus": [], "processes": [], "ts": time.time()}
        except Exception as exc:  # pragma: no cover - defensive
            self._warn_throttled("GPU collection error: %s", exc)
            data = {"available": False, "reason": f"collector error: {exc}", "gpus": [], "processes": [], "ts": time.time()}
        self._cache = data
        return data

    async def _collect(self) -> dict:
        data: Optional[dict] = None
        # 1) local NVML — authoritative when running on the GPU host
        if self._probe_nvml():
            try:
                data = await self._collect_nvml()
            except GPUNotAvailable as exc:
                self._warn_throttled("NVML unavailable (%s); trying nvidia-smi", exc)
            except Exception as exc:  # pragma: no cover
                self._warn_throttled("NVML collection failed, falling back to nvidia-smi: %s", exc)

        # 2) local nvidia-smi
        if data is None:
            smi = self._find_nvidia_smi()
            if smi:
                try:
                    data = await self._collect_smi(smi)
                except GPUNotAvailable as exc:
                    self._warn_throttled("nvidia-smi failed: %s", exc)

        # 3) optional remote sysinfo endpoint (only if explicitly configured)
        if data is None and self.sysinfo_url:
            try:
                data = await self._collect_remote()
            except Exception as exc:
                self._warn_throttled("Remote GPU collector unavailable: %s", exc)

        if data is None:
            raise GPUNotAvailable("no NVIDIA GPU detected (nvidia-smi / NVML not found)")

        await self._attach_fan_state(data)
        return data

    async def _attach_fan_state(self, data: dict) -> None:
        """Attach v100-fan control state to V100 GPUs.

        The Tesla V100 fan is governed by `v100-fan.service` (ESP32 controller),
        NOT by NVML fan speed. We display fan_target / fan_pwm from that system
        and never surface the NVML fan value as V100 fan control.
        """
        from .v100_fan import get_v100_fan_reader

        fan = await get_v100_fan_reader().read_state()
        gpus = data.get("gpus", [])
        for g in gpus:
            is_v100 = "V100" in (g.get("name") or "").upper() or len(gpus) == 1
            if is_v100:
                g["fan_target"] = fan.get("fan_target")
                g["fan_pwm"] = fan.get("fan_pwm")
                g["fan_source"] = fan.get("source")
                g["fan_available"] = fan.get("available", False)
                g["fan_reason"] = fan.get("reason")
            else:
                g["fan_target"] = None
                g["fan_pwm"] = None
                g["fan_source"] = None
                g["fan_available"] = False
                g["fan_reason"] = "fan control not managed by v100-fan"

    # ---- nvidia-smi ------------------------------------------------------------
    async def _run(self, cmd: list[str], timeout: float = 5.0) -> str:
        """Run one subprocess with array args, no shell. Cached discovery."""
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            raise GPUNotAvailable("nvidia-smi timed out")
        if proc.returncode != 0:
            raise GPUNotAvailable(
                stderr.decode(errors="replace").strip() or f"nvidia-smi exited {proc.returncode}"
            )
        return stdout.decode(errors="replace")

    async def _collect_smi(self, smi: str) -> dict:
        csv = await self._run(
            [smi, "--query-gpu=" + _GPU_QUERY, "--format=csv,noheader,nounits"]
        )
        gpus = [_parse_smi_gpu(line) for line in csv.splitlines() if line.strip()]
        if not gpus:
            raise GPUNotAvailable("nvidia-smi returned no GPUs")
        procs_csv = ""
        try:
            procs_csv = await self._run(
                [smi, "--query-compute-apps=" + _PROCS_QUERY, "--format=csv,noheader,nounits"]
            )
        except GPUNotAvailable:
            pass
        processes = [_parse_smi_proc(line) for line in procs_csv.splitlines() if line.strip()]
        driver = gpus[0].get("driver_version", "") if gpus else ""
        return {
            "available": True,
            "source": "nvidia-smi",
            "driver_version": driver,
            "cuda_version": _cuda_version(driver),
            "gpus": gpus,
            "processes": processes,
            "ts": time.time(),
        }

    # ---- NVML ----------------------------------------------------------------
    async def _collect_nvml(self) -> dict:
        """Collect GPU data via NVML.

        Every metric is fetched independently and never raises for a single
        failed field — that field becomes None. Raises GPUNotAvailable only if
        no device can be enumerated at all.
        """
        import pynvml

        def _sync() -> dict:
            pynvml.nvmlInit()
            try:
                count = _i(pynvml.nvmlDeviceGetCount)
                if count is None or count <= 0:
                    raise GPUNotAvailable("NVML: no devices returned")
                gpus = []
                processes = []
                for i in range(count):
                    handle = _call(pynvml.nvmlDeviceGetHandleByIndex, i)
                    if handle is None:
                        continue
                    gpus.append({
                        "index": i,
                        "name": _s(pynvml.nvmlDeviceGetName, handle) or "GPU",
                        "driver_version": _s(pynvml.nvmlSystemGetDriverVersion) or "",
                        "cuda_version": _cuda_str(_call(pynvml.nvmlSystemGetCudaDriverVersion_v2)),
                        "temperature": _i(pynvml.nvmlDeviceGetTemperature, handle, pynvml.NVML_TEMPERATURE_GPU),
                        "utilization": _struct_attr(pynvml.nvmlDeviceGetUtilizationRates, "gpu", handle),
                        "memory_utilization": _struct_attr(pynvml.nvmlDeviceGetUtilizationRates, "memory", handle),
                        "vram_total": _struct_attr(pynvml.nvmlDeviceGetMemoryInfo, "total", handle),
                        "vram_used": _struct_attr(pynvml.nvmlDeviceGetMemoryInfo, "used", handle),
                        "vram_free": _struct_attr(pynvml.nvmlDeviceGetMemoryInfo, "free", handle),
                        "power_draw": _mw_to_w(pynvml.nvmlDeviceGetPowerUsage, handle),
                        "power_limit": _mw_to_w(pynvml.nvmlDeviceGetEnforcedPowerLimit, handle),
                        # fan is NOT taken from NVML for the V100 — see _attach_fan_state
                        "clocks": _i(pynvml.nvmlDeviceGetClockInfo, handle, pynvml.NVML_CLOCK_SM),
                        "mem_clock": _i(pynvml.nvmlDeviceGetClockInfo, handle, pynvml.NVML_CLOCK_MEM),
                        "pstate": _i(pynvml.nvmlDeviceGetPerformanceState, handle),
                        "pcie_link": _i(pynvml.nvmlDeviceGetCurrPcieLinkGeneration, handle),
                        "pcie_width": _i(pynvml.nvmlDeviceGetCurrPcieLinkWidth, handle),
                        "pci_bus": _pci_bus_str(_call(pynvml.nvmlDeviceGetPciInfo, handle)),
                        # not exposed via the simple NVML getters used here
                        "compute_mode": None,
                        "persistence_mode": None,
                        "display_active": None,
                    })
                    processes.extend(_nvml_processes(pynvml, handle, i))
                if not gpus:
                    raise GPUNotAvailable("NVML: failed to enumerate devices")
                return {
                    "available": True,
                    "source": "NVML",
                    "driver_version": gpus[0].get("driver_version", ""),
                    "gpus": gpus,
                    "processes": processes,
                    "ts": time.time(),
                }
            finally:
                try:
                    pynvml.nvmlShutdown()
                except Exception:
                    pass

        return await asyncio.to_thread(_sync)

    # ---- remote sysinfo ---------------------------------------------------------
    async def _collect_remote(self) -> dict | None:
        async with httpx.AsyncClient(timeout=3.0) as client:
            for path in ("/gpu", "/gpu/info", "/gpus", "/api/gpu"):
                try:
                    resp = await client.get(self.sysinfo_url + path)
                    if resp.status_code == 200:
                        data = resp.json()
                        if isinstance(data, dict) and ("gpus" in data or "gpu" in data):
                            data["source"] = "remote"
                            data["available"] = True
                            data["ts"] = time.time()
                            return data
                except Exception:
                    continue
        return None

    # ---- extra -------------------------------------------------------------------
    async def gpu_processes(self) -> dict:
        data = await self.sample()
        return {"available": data.get("available", False), "processes": data.get("processes", [])}

    async def ollama_vram(self, running: list[dict]) -> dict:
        """VRAM usage attributed to loaded Ollama models (from /api/ps)."""
        total = sum(m.get("size_vram", 0) for m in running)
        per_model = {m.get("name", ""): m.get("size_vram", 0) for m in running}
        return {"total_vram": total, "per_model": per_model}


# ---- NVML per-field helpers ------------------------------------------------------
def _call(fn, *args):
    """Call an NVML getter; return None on any error."""
    try:
        return fn(*args)
    except Exception:
        return None


def _i(fn, *args):
    """Getter that must return an int (not the NVML sentinel)."""
    v = _call(fn, *args)
    if isinstance(v, int) and v != NVML_NOT_AVAILABLE:
        return v
    return None


def _s(fn, *args):
    """Getter that must return a string (handles NVML bytes)."""
    v = _call(fn, *args)
    if isinstance(v, bytes):
        return v.decode(errors="replace")
    if isinstance(v, str):
        return v
    return None


def _struct_attr(fn, attr, *args):
    """Getter that returns a struct; extract one attribute (or None)."""
    v = _call(fn, *args)
    if v is None:
        return None
    val = getattr(v, attr, None)
    if isinstance(val, int) and val == NVML_NOT_AVAILABLE:
        return None
    return val


def _mw_to_w(fn, *args):
    """NVML power values are in milliwatts; convert to Watts (or None)."""
    v = _call(fn, *args)
    if isinstance(v, int) and v != NVML_NOT_AVAILABLE:
        return v / 1000.0
    return None


def _cuda_str(v):
    """NVML CUDA driver version is an int like 12020 → '12.20'."""
    if isinstance(v, int) and v > 0:
        return f"{v // 1000}.{v % 100}"
    return None


def _nvml_processes(pynvml, handle, gpu_index: int) -> list[dict]:
    """Compute + graphics running processes for one device."""
    out = []
    for getter in (
        getattr(pynvml, "nvmlDeviceGetComputeRunningProcesses", None),
        getattr(pynvml, "nvmlDeviceGetGraphicsRunningProcesses", None),
    ):
        if getter is None:
            continue
        try:
            for p in getter(handle):
                pid = getattr(p, "pid", None)
                name = None
                if pid is not None:
                    name = _s(pynvml.nvmlSystemGetProcessName, pid)
                if not name:
                    name = _s(lambda: getattr(p, "processName", None))
                used = getattr(p, "usedGpuMemory", None)
                if isinstance(used, int) and used == NVML_NOT_AVAILABLE:
                    used = None
                out.append({
                    "pid": pid,
                    "name": name or "unknown",
                    "used_memory": used,
                    "gpu_index": gpu_index,
                })
        except Exception:
            continue
    return out


# ---- nvidia-smi parsing helpers ---------------------------------------------------
def _parse_smi_gpu(line: str) -> dict:
    parts = [p.strip() for p in line.split(",")]
    def num(i: int, default=None):
        try:
            v = parts[i]
            f = float(v)
            return int(f) if f.is_integer() else f
        except (IndexError, ValueError):
            return default
    p = parts
    return {
        "index": num(0),
        "name": p[1] if len(p) > 1 else "",
        "uuid": p[2] if len(p) > 2 else "",
        "driver_version": p[3] if len(p) > 3 else "",
        "temperature": num(4),
        "utilization": num(5),
        "memory_utilization": num(6),
        "vram_total": _bytes_from_mib(num(7)) if num(7) is not None else None,
        "vram_used": _bytes_from_mib(num(8)) if num(8) is not None else None,
        "vram_free": _bytes_from_mib(num(9)) if num(9) is not None else None,
        "power_draw": num(10) if num(10) is not None else None,
        "power_limit": num(11) if num(11) is not None else None,
        "pstate": p[12] if len(p) > 12 else "",
        "clocks": num(13),
        "mem_clock": num(14),
        "clocks_max": num(15),
        "mem_clock_max": num(16),
        "pcie_gen": num(18),
        "pcie_width": num(19),
        "pcie_gen_max": num(20),
        "pcie_width_max": num(21),
        "compute_mode": p[22] if len(p) > 22 else "",
        "persistence_mode": p[23] if len(p) > 23 else "",
        "display_active": p[24] if len(p) > 24 else "",
    }


def _parse_smi_proc(line: str) -> dict:
    parts = [p.strip() for p in line.split(",")]
    def num(i: int, default=None):
        try:
            return int(float(parts[i]))
        except (IndexError, ValueError):
            return default
    return {
        "pid": num(0),
        "name": parts[1] if len(parts) > 1 else "",
        "used_memory": _bytes_from_mib(num(2)) if num(2) is not None else None,
        "gpu_uuid": parts[3] if len(parts) > 3 else "",
    }


def _bytes_from_mib(v):
    if v is None:
        return None
    return int(float(v) * 1024 * 1024)


def _pci_bus_str(pci) -> str:
    try:
        return f"{pci.domain:04X}:{pci.bus:02X}:{pci.device:02X}.{pci.function}"
    except Exception:
        return ""


def _cuda_version(driver: str) -> str:
    """Best-effort CUDA version from the driver (not always reliable)."""
    return "" if not driver else f"{driver} (driver)"


# singleton
_collector: Optional[GPUCollector] = None


def get_gpu_collector() -> GPUCollector:
    global _collector
    if _collector is None:
        _collector = GPUCollector()
    return _collector
