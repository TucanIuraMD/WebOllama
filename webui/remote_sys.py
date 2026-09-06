"""Local processor statistics — CPU/GPU/Ollama of THE machine running WebOllama.

Architecture note (v2): WebOllama is designed to be deployed ON the Ollama
server. The Dashboard PROCESSOR block therefore shows the LOCAL host's data:

- CPU / RAM  -> existing SystemCollector (psutil)  [webui/sys_collector.py]
- GPU / VRAM -> existing GPUCollector (NVML primary, nvidia-smi fallback;
               returns {"available": false} when no NVIDIA GPU is present)
               [webui/gpu_collector.py]
- Models     -> existing OllamaClient (/api/ps), i.e. the data behind
               `ollama ps`. When the Ollama API is unreachable but the
               `ollama` CLI exists, a local `ollama ps --format json`
               fallback is used (same host, no SSH, no remote commands).

Nothing here calls remote machines. When a piece is unavailable it degrades
to a structured "unavailable" state — never a traceback, never fabricated
values.
"""
import asyncio
import json
import logging
import shutil
import time
from typing import Any, Optional

from .gpu_collector import get_gpu_collector
from .ollama_client import OllamaClient, OllamaError, get_client
from .sys_collector import get_system_collector

logger = logging.getLogger(__name__)

CLI_TIMEOUT = 4.0  # seconds for the `ollama ps` CLI fallback


def _f(value) -> Optional[float]:
    try:
        if value is None or isinstance(value, bool):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _clean_gpu(raw: dict, i: int) -> dict:
    g = raw if isinstance(raw, dict) else {}
    name = g.get("name")
    mu, mt = _f(g.get("vram_used", g.get("memory_used"))), _f(g.get("vram_total", g.get("memory_total")))
    util = _f(g.get("utilization"))
    return {
        "index": g.get("index", i),
        "name": str(name) if name else f"GPU {i}",
        "utilization": util,
        "memory_used": mu,
        "memory_total": mt,
        "memory_utilization": _f(g.get("memory_utilization"))
        or ((mu / mt * 100.0) if mu is not None and mt else None),
        "temperature": _f(g.get("temperature")),
    }


class ProcessorCollector:
    """Aggregates local CPU + GPU + Ollama running models into one payload."""

    def __init__(self, ollama: Optional[OllamaClient] = None,
                 gpu: Optional[Any] = None, system: Optional[Any] = None) -> None:
        self._ollama = ollama
        self._gpu = gpu
        self._system = system
        self._lock: Optional[asyncio.Lock] = None
        self._cache: Optional[dict] = None
        self._cache_ts = 0.0
        self._cache_ttl = 2.0  # poll guidance is 2-5s; cache below that

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    # ---- collaborators -------------------------------------------------------
    @property
    def ollama(self) -> OllamaClient:
        return self._ollama if self._ollama is not None else get_client()

    @property
    def gpu(self):
        return self._gpu if self._gpu is not None else get_gpu_collector()

    @property
    def system(self):
        return self._system if self._system is not None else get_system_collector()

    # ---- local ollama ps CLI fallback ----------------------------------------
    async def _cli_ps(self) -> Optional[list]:
        """`ollama ps --format json` on THIS host; None when unusable.

        Used only when the Ollama HTTP API is unreachable while the CLI
        exists locally — e.g. right after the project is moved to the
        Ollama server and before the API is reachable.
        """
        if not shutil.which("ollama"):
            return None
        try:
            proc = await asyncio.create_subprocess_exec(
                "ollama", "ps", "--format", "json",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                out, _err = await asyncio.wait_for(proc.communicate(), timeout=CLI_TIMEOUT)
            except asyncio.TimeoutError:
                proc.kill()
                return None
            if proc.returncode != 0:
                return None
            data = json.loads(out.decode() or "[]")
            rows = data if isinstance(data, list) else []
            return rows if all(isinstance(r, dict) for r in rows) else None
        except Exception:  # noqa: BLE001 — CLI fallback must never crash
            return None

    @staticmethod
    def _models_from_ps(running: list) -> list:
        """Map /api/ps (== `ollama ps`) rows into a compact model list."""
        models = []
        for m in running or []:
            if not isinstance(m, dict):
                continue
            models.append({
                "name": m.get("name") or m.get("model") or "unknown",
                "size_vram": _f(m.get("size_vram")) or 0,
                "size": _f(m.get("size")) or 0,
            })
        return models

    # ---- main entry ----------------------------------------------------------
    async def sample(self, force: bool = False) -> dict:
        if self._cache is not None and not force and (time.time() - self._cache_ts) < self._cache_ttl:
            return self._cache
        async with self._get_lock():
            if self._cache is not None and not force and (time.time() - self._cache_ts) < self._cache_ttl:
                return self._cache
            gpu_task = asyncio.create_task(self.gpu.sample())
            sys_task = asyncio.create_task(self.system.sample())
            gpu_data, sysdata = await asyncio.gather(gpu_task, sys_task)

            # running models: prefer the Ollama API (same data as `ollama ps`);
            # if the API is down, try the LOCAL `ollama ps` CLI once.
            ollama_status = await self.ollama.status()
            cli_used = False
            if ollama_status.get("online"):
                running = ollama_status.get("running", [])
            else:
                running = await self._cli_ps() or []
                cli_used = bool(running)

            vram_used = sum(m["size_vram"] for m in self._models_from_ps(running))
            gpus = [_clean_gpu(g, i) for i, g in enumerate(gpu_data.get("gpus", []) or [])]
            cpu_raw = sysdata.get("cpu", {}) or {}
            load = cpu_raw.get("load") or [cpu_raw.get("load_1"), cpu_raw.get("load_5"), cpu_raw.get("load_15")]
            load = [_f(x) for x in (list(load)[:3] if isinstance(load, (list, tuple)) else [])]
            while len(load) < 3:
                load.append(None)

            out = {
                "available": bool(gpu_data.get("available") or cpu_raw.get("percent") is not None or running),
                "source": "local",
                "host": sysdata.get("hostname") or "local",
                "cpu": {
                    "utilization": _f(cpu_raw.get("percent")),
                    "cores_physical": cpu_raw.get("cores") or None,
                    "cores_logical": cpu_raw.get("threads") or None,
                    "load": load,
                },
                "gpus": gpus,
                "gpu_available": bool(gpu_data.get("available")),
                "gpu_reason": None if gpu_data.get("available") else (gpu_data.get("reason") or "no GPU data"),
                "ollama": {
                    "online": bool(ollama_status.get("online") or cli_used),
                    "api_online": bool(ollama_status.get("online")),
                    "cli_fallback_used": cli_used,
                    "endpoint": ollama_status.get("endpoint") or "",
                    "running_models": self._models_from_ps(running),
                    "vram_used": vram_used or 0,
                },
                "ts": time.time(),
            }
            if not out["available"]:
                bits = []
                if not gpu_data.get("available"):
                    bits.append(gpu_data.get("reason") or "GPU unavailable")
                if cpu_raw.get("percent") is None:
                    bits.append("CPU data unavailable")
                if not (ollama_status.get("online") or running):
                    bits.append("Ollama offline")
                out["reason"] = "; ".join(bits) or "no processor data"
            self._cache, self._cache_ts = out, time.time()
            return out


_processor: Optional[ProcessorCollector] = None


def get_processor_collector() -> ProcessorCollector:
    global _processor
    if _processor is None:
        _processor = ProcessorCollector()
    return _processor
