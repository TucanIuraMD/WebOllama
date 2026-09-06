"""Remote processor statistics collector — CPU/GPU of the OLLAMA server.

WebOllama runs on one host (e.g. 192.168.80.111) while Ollama + the NVIDIA
GPU(s) live on another (e.g. 192.168.80.22). This module fetches processor
statistics from a minimal host-agent running THERE (no SSH, no root, no
secrets, no Ollama API involvement).

Endpoint contract (served by scripts/ollama22-host-agent.py):
    GET {base}/api/processor ->
    {
      "host": "192.168.80.22",
      "cpu":  {"utilization": 42.0, "cores_physical": 8, "cores_logical": 16,
               "load": [3.2, 2.8, 2.4]},
      "gpus": [{"index": 0, "name": "Tesla V100-SXM2-16GB", "utilization": 74.0,
                "memory_used": 12400000000, "memory_total": 16160000000,
                "memory_utilization": 46.0, "temperature": 64}],
      "timestamp": "2026-09-06T14:00:00Z"
    }

Robustness contract mirrors gpu_collector.py:
- unreachable host / bad status / malformed body -> {"available": false,
  "reason": ...} and the last good payload as "last_good" (max 60s old);
- one bad GPU field becomes None, never a crash, never fabricated values;
- single-flight + short cache to protect the remote agent from per-second forks.
"""
import logging
import time
from typing import Optional

import httpx

from .config import PROCESSOR_URL, SYSINFO_URL

logger = logging.getLogger(__name__)

LAST_GOOD_TTL = 60.0        # serve last good payload at most this long
_WARN_INTERVAL = 30.0       # throttle repeated failure warnings


def _num(value):
    """Coerce to float or None — a single bad field must never crash."""
    try:
        if value is None or isinstance(value, bool):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _clean_gpu(raw: dict, i: int) -> dict:
    g = raw if isinstance(raw, dict) else {}
    name = g.get("name")
    mu, mt = _num(g.get("memory_used")), _num(g.get("memory_total"))
    return {
        "index": g.get("index", i),
        "name": str(name) if name else f"GPU {i}",
        "utilization": _num(g.get("utilization")),
        "memory_used": mu,
        "memory_total": mt,
        "memory_utilization": _num(g.get("memory_utilization"))
        or ((mu / mt * 100.0) if mu is not None and mt else None),
        "temperature": _num(g.get("temperature")),
    }


def _clean_cpu(raw) -> dict:
    c = raw if isinstance(raw, dict) else {}
    load = c.get("load") if isinstance(c.get("load"), (list, tuple)) else None
    return {
        "utilization": _num(c.get("utilization")),
        "cores_physical": int(c["cores_physical"]) if _num(c.get("cores_physical")) is not None else None,
        "cores_logical": int(c["cores_logical"]) if _num(c.get("cores_logical")) is not None else None,
        "load": [_num(x) for x in load] if load else None,
    }


class RemoteProcessorCollector:
    def __init__(self, base_url: str = "") -> None:
        self.base_url = (base_url or PROCESSOR_URL or SYSINFO_URL or "").rstrip("/")
        self._client: Optional[httpx.AsyncClient] = None
        self._lock = None  # lazily created inside a running loop
        self._cache: Optional[dict] = None
        self._cache_ts = 0.0
        self._cache_ttl = 2.0   # poll guidance is 2-5s; cache below that
        self._last_good: Optional[dict] = None
        self._last_good_ts = 0.0
        self._last_warn = 0.0

    def _get_lock(self):
        if self._lock is None:
            import asyncio

            self._lock = asyncio.Lock()
        return self._lock

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url or "http://invalid",
                timeout=httpx.Timeout(2.5),
            )
        return self._client

    async def aclose(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    def _warn_throttled(self, msg: str) -> None:
        now = time.monotonic()
        if now - self._last_warn >= _WARN_INTERVAL:
            self._last_warn = now
            logger.warning(msg)

    def _unavailable(self, reason: str) -> dict:
        lg, lg_ts = self._last_good, self._last_good_ts
        fresh = lg is not None and (time.time() - lg_ts) <= LAST_GOOD_TTL
        out = {"available": False, "source": "remote", "configured": bool(self.base_url),
               "reason": reason, "host": (lg or {}).get("host") or self.base_url,
               "cpu": None, "gpus": [], "ts": time.time()}
        if fresh:
            out.update({"cpu": lg.get("cpu"), "gpus": lg.get("gpus", []),
                        "stale": True, "stale_age": round(time.time() - lg_ts, 1),
                        "last_good_ts": lg_ts})
        return out

    def _normalize(self, data) -> Optional[dict]:
        if not isinstance(data, dict):
            return None
        cpu = _clean_cpu(data.get("cpu"))
        raw_gpus = data.get("gpus")
        if raw_gpus is None:
            raw_gpus = []
        if not isinstance(raw_gpus, list):
            raw_gpus = []
        host = data.get("host") or self.base_url
        return {
            "available": True,
            "source": "remote",
            "host": host,
            "cpu": cpu,
            "gpus": [_clean_gpu(g, i) for i, g in enumerate(raw_gpus)],
            "timestamp": data.get("timestamp"),
            "ts": time.time(),
            "stale": False,
        }

    async def sample(self, force: bool = False) -> dict:
        if not self.base_url:
            return self._unavailable("remote processor source not configured (PROCESSOR_URL)")
        async with self._get_lock():
            now = time.time()
            if not force and self._cache and (now - self._cache_ts) < self._cache_ttl:
                return self._cache
            try:
                client = await self._get_client()
                resp = await client.get("/api/processor")
                if resp.status_code != 200:
                    out = self._unavailable(f"remote agent HTTP {resp.status_code}")
                else:
                    try:
                        data = resp.json()
                    except Exception:
                        out = self._unavailable("remote agent returned malformed JSON")
                    else:
                        norm = self._normalize(data)
                        if norm is None:
                            out = self._unavailable("remote agent payload malformed")
                        else:
                            self._last_good, self._last_good_ts = norm, now
                            out = norm
            except (httpx.HTTPError, OSError) as exc:
                out = self._unavailable(f"remote host unreachable: {type(exc).__name__}")
            except Exception as exc:  # noqa: BLE001 — never break the caller
                out = self._unavailable(f"remote processor error: {exc}")
            if out.get("available"):
                self._cache, self._cache_ts = out, now
            else:
                self._cache, self._cache_ts = None, 0.0  # retry next call
                self._warn_throttled(f"remote processor unavailable: {out.get('reason')}")
            return out


_processor: Optional[RemoteProcessorCollector] = None


def get_remote_processor() -> RemoteProcessorCollector:
    global _processor
    if _processor is None:
        _processor = RemoteProcessorCollector()
    return _processor
