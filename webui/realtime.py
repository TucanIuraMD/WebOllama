"""Realtime snapshot builder — collects GPU/CPU/RAM/disk/network/Ollama/jobs
into one JSON document pushed over WebSocket every refresh interval."""
import asyncio
import logging
import time
from typing import Optional

from .config import METRICS_INTERVAL, REFRESH_INTERVAL
from .db import Database
from .gpu_collector import GPUCollector
from .jobs import JobManager
from .ollama_client import OllamaClient, OllamaError
from .sys_collector import SystemCollector

logger = logging.getLogger(__name__)


class RealtimeService:
    def __init__(
        self,
        db: Database,
        client: OllamaClient,
        gpu: GPUCollector,
        system: SystemCollector,
        jobs: JobManager,
        ws_broadcast,
    ) -> None:
        self.db = db
        self.client = client
        self.gpu = gpu
        self.system = system
        self.jobs = jobs
        self.ws_broadcast = ws_broadcast
        self._tasks: list[asyncio.Task] = []
        self.last_snapshot: dict = {}
        self._snapshot_lock = asyncio.Lock()
        # cache Ollama status between snapshots (cheap refresh)
        self._ollama_cache: dict = {}
        self._ollama_cache_ts = 0.0
        self._ollama_ttl = 2.0

    async def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self._realtime_loop(), name="realtime"),
            asyncio.create_task(self._metrics_loop(), name="metrics"),
        ]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _ollama_status(self) -> dict:
        now = time.time()
        if now - self._ollama_cache_ts >= self._ollama_ttl:
            self._ollama_cache = await self.client.status()
            self._ollama_cache_ts = now
        return self._ollama_cache

    async def build_snapshot(self, persist: bool = False) -> dict:
        ollama = await self._ollama_status()
        gpu_task = asyncio.create_task(self.gpu.sample())
        sys_task = asyncio.create_task(self.system.sample())
        jobs_list = await self.jobs.list(limit=30)
        gpu, sysdata = await asyncio.gather(gpu_task, sys_task)

        # merge Ollama VRAM attribution into GPU view
        if ollama.get("online"):
            vram = await self.gpu.ollama_vram(ollama.get("running", []))
            gpu["ollama_vram"] = vram
        else:
            gpu["ollama_vram"] = {"total_vram": 0, "per_model": {}}

        ollama_proc = await self.system.ollama_process()

        # LLM API endpoint statuses (cached by LLMManager's background task)
        llm_snapshot = []
        try:
            from .llm_api import get_llm_manager
            llm_snapshot = await get_llm_manager().snapshot()
        except Exception as exc:  # pragma: no cover
            logger.debug("llm snapshot failed: %s", exc)

        snapshot = {
            "ts": time.time(),
            "gpu": gpu,
            "cpu": sysdata.get("cpu", {}),
            "ram": sysdata.get("ram", {}),
            "disk": sysdata.get("disk", {}),
            "network": sysdata.get("network", {}),
            "swap": sysdata.get("swap", {}),
            "system": {
                "hostname": sysdata.get("hostname", ""),
                "os": sysdata.get("os", ""),
                "uptime": sysdata.get("uptime", 0),
                "ollama_proc": ollama_proc,
            },
            "ollama": ollama,
            "llm": llm_snapshot,
            "jobs": jobs_list,
        }
        self.last_snapshot = snapshot

        if persist:
            ts = time.time()
            try:
                await self.db.insert_metric(ts, "gpu", {
                    "available": gpu.get("available"),
                    "gpus": [{k: g.get(k) for k in ("index", "name", "utilization", "memory_utilization", "temperature", "vram_used", "vram_total", "power_draw", "fan_target", "fan_pwm")} for g in gpu.get("gpus", [])],
                    "ollama_vram": gpu.get("ollama_vram", {}),
                })
                await self.db.insert_metric(ts, "system", {
                    "cpu": sysdata.get("cpu", {}).get("percent"),
                    "load": list(sysdata.get("cpu", {}).get("load", (0, 0, 0))),
                    "ram_used": sysdata.get("ram", {}).get("used"),
                    "ram_total": sysdata.get("ram", {}).get("total"),
                    "swap_used": sysdata.get("swap", {}).get("used"),
                    "disk_used": sysdata.get("disk", {}).get("used"),
                    "disk_total": sysdata.get("disk", {}).get("total"),
                    "rx_rate": sysdata.get("network", {}).get("rx_rate"),
                    "tx_rate": sysdata.get("network", {}).get("tx_rate"),
                })
            except Exception as exc:  # pragma: no cover
                logger.debug("metric persist failed: %s", exc)
        return snapshot

    async def _realtime_loop(self) -> None:
        logger.info("Realtime loop started (interval=%.1fs)", REFRESH_INTERVAL)
        while True:
            try:
                snap = await self.build_snapshot()
                await self.ws_broadcast({"type": "snapshot", **snap})
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("realtime loop error: %s", exc)
            await asyncio.sleep(REFRESH_INTERVAL)

    async def _metrics_loop(self) -> None:
        logger.info("Metrics persistence loop started (interval=%.1fs)", METRICS_INTERVAL)
        while True:
            try:
                await self.build_snapshot(persist=True)
                await self.db.prune_old_metrics()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("metrics loop error: %s", exc)
            await asyncio.sleep(METRICS_INTERVAL)


_service: Optional[RealtimeService] = None


def get_realtime_service() -> RealtimeService:
    assert _service is not None, "RealtimeService not initialized"
    return _service


def set_realtime_service(svc: RealtimeService) -> None:
    global _service
    _service = svc
