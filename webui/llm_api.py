"""LLM API endpoint management — OpenAI-compatible providers.

Provides a provider abstraction (Ollama / OmniRouter / generic OpenAI-compatible)
so new endpoints (OpenRouter, vLLM, LiteLLM, OpenAI, ...) can be added later
without frontend changes.

Security contract:
- API keys are stored in SQLite, never logged, never returned in full via REST.
- The backend adds the Authorization header itself.
- SSRF protection: the backend only ever connects to base URLs stored in the
  endpoint configuration; check/models requests reference an endpoint by id and
  never accept an arbitrary URL from the frontend.
"""
import asyncio
import json
import logging
import time
from typing import Any, Optional

import httpx

from .db import Database

logger = logging.getLogger(__name__)

SETTINGS_KEY = "llm_endpoints"

# Default endpoints (same server as WebOllama — 192.168.80.22).
DEFAULT_ENDPOINTS: list[dict] = [
    {
        "id": "ollama",
        "name": "Ollama",
        "type": "openai-compatible",
        "base_url": "http://192.168.80.22:11434/v1",
        "api_key": "",
        "enabled": True,
    },
    {
        "id": "omnirouter",
        "name": "OmniRouter",
        "type": "openai-compatible",
        "base_url": "http://192.168.80.22:20128/v1",
        "api_key": "",
        "enabled": True,
    },
]

CHECK_INTERVAL = 15.0      # seconds between background status checks
CHECK_TIMEOUT = 20.0       # per-endpoint check timeout
MODELS_TIMEOUT = 30.0      # /models request timeout


class LLMError(Exception):
    pass


def mask_api_key(key: str) -> str:
    """Mask a key for display — never leak the full secret."""
    if not key:
        return ""
    if len(key) <= 8:
        return "*" * len(key)
    return key[:3] + "****" + key[-4:]


def validate_endpoint_url(url: str) -> str:
    """SSRF guard: only http/https, with a host, on a well-formed URL."""
    from urllib.parse import urlparse

    url = (url or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise LLMError("invalid URL scheme (http/https required)")
    if not parsed.hostname:
        raise LLMError("invalid URL (no host)")
    if len(url) > 2000:
        raise LLMError("URL too long")
    return url.rstrip("/")


# ---- Provider base -----------------------------------------------------------
class LLMProvider:
    """Base class for OpenAI-compatible providers."""

    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self.id = str(cfg.get("id", "llm"))
        self.name = str(cfg.get("name", self.id))
        self.base_url = str(cfg.get("base_url", "")).rstrip("/")
        self.api_key = str(cfg.get("api_key", "") or "")
        self.enabled = bool(cfg.get("enabled", True))

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    async def _get(self, path: str, timeout: float) -> httpx.Response:
        async with httpx.AsyncClient(timeout=timeout, headers=self._headers()) as client:
            return await client.get(self.base_url + path)

    async def check(self) -> dict:
        """GET /models → status with latency and model count. Never raises."""
        started = time.monotonic()
        result: dict = {
            "id": self.id,
            "name": self.name,
            "online": False,
            "http_status": None,
            "latency_ms": None,
            "models_count": None,
            "error": None,
            "checked_at": None,
        }
        try:
            resp = await self._get("/models", CHECK_TIMEOUT)
            result["latency_ms"] = round((time.monotonic() - started) * 1000, 1)
            result["http_status"] = resp.status_code
            result["checked_at"] = time.time()
            if resp.status_code >= 400:
                result["error"] = f"HTTP {resp.status_code}: {resp.text[:200]}"
                return result
            data = resp.json()
            models = data.get("data", []) if isinstance(data, dict) else []
            result["models_count"] = len(models)
            result["online"] = True
        except httpx.ConnectError as exc:
            result["error"] = f"connection failed: {exc}"
        except httpx.TimeoutException:
            result["error"] = "timeout"
        except httpx.HTTPError as exc:
            result["error"] = str(exc)
        except (ValueError, TypeError) as exc:
            result["error"] = f"invalid response: {exc}"
        result["checked_at"] = time.time()
        return result

    async def models(self) -> dict:
        """GET /models → parsed model list (preserves extra fields)."""
        started = time.monotonic()
        try:
            resp = await self._get("/models", MODELS_TIMEOUT)
            latency = round((time.monotonic() - started) * 1000, 1)
            if resp.status_code >= 400:
                return {"ok": False, "error": f"HTTP {resp.status_code}: {resp.text[:200]}",
                        "models": [], "http_status": resp.status_code, "latency_ms": latency}
            data = resp.json()
            models = data.get("data", []) if isinstance(data, dict) else []
            return {"ok": True, "models": models, "count": len(models),
                    "http_status": resp.status_code, "latency_ms": latency}
        except httpx.ConnectError as exc:
            return {"ok": False, "error": f"connection failed: {exc}", "models": [], "http_status": None}
        except httpx.TimeoutException:
            return {"ok": False, "error": "timeout", "models": [], "http_status": None}
        except httpx.HTTPError as exc:
            return {"ok": False, "error": str(exc), "models": [], "http_status": None}
        except (ValueError, TypeError) as exc:
            return {"ok": False, "error": f"invalid response: {exc}", "models": [], "http_status": None}


class OllamaProvider(LLMProvider):
    """Ollama OpenAI-compatible endpoint (http://host:11434/v1)."""


class OmniRouterProvider(LLMProvider):
    """OmniRouter OpenAI-compatible endpoint (http://host:20128/v1)."""


def make_provider(cfg: dict) -> LLMProvider:
    ptype = str(cfg.get("type", "openai-compatible")).lower()
    if ptype == "ollama":
        return OllamaProvider(cfg)
    if ptype == "omnirouter":
        return OmniRouterProvider(cfg)
    return LLMProvider(cfg)


# ---- Manager ------------------------------------------------------------------
class LLMManager:
    def __init__(self, db: Database) -> None:
        self.db = db
        self._status_cache: dict[str, dict] = {}
        self._task: Optional[asyncio.Task] = None

    # ---- config ------------------------------------------------------------------
    async def load_endpoints(self) -> list[dict]:
        raw = await self.db.get_setting(SETTINGS_KEY, "")
        if not raw:
            eps = [dict(e) for e in DEFAULT_ENDPOINTS]
            await self.db.set_setting(SETTINGS_KEY, json.dumps(eps))
            return eps
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return parsed
        except ValueError:
            pass
        return [dict(e) for e in DEFAULT_ENDPOINTS]

    async def save_endpoints(self, eps: list[dict]) -> None:
        await self.db.set_setting(SETTINGS_KEY, json.dumps(eps))

    async def get_endpoint(self, eid: str) -> Optional[dict]:
        for e in await self.load_endpoints():
            if e.get("id") == eid:
                return e
        return None

    def public_endpoint(self, cfg: dict) -> dict:
        """Endpoint config with the API key masked (never the full secret)."""
        out = {k: v for k, v in cfg.items() if k != "api_key"}
        out["api_key"] = mask_api_key(str(cfg.get("api_key", "") or ""))
        return out

    # ---- lifecycle -----------------------------------------------------------------
    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop())
        # fire an initial check so statuses appear quickly
        try:
            await self.check_all()
        except Exception as exc:  # pragma: no cover
            logger.debug("initial llm check failed: %s", exc)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                await self.check_all()
            except Exception as exc:
                logger.debug("llm check loop error: %s", exc)
            await asyncio.sleep(CHECK_INTERVAL)

    # ---- status ---------------------------------------------------------------------
    async def check_all(self) -> dict[str, dict]:
        eps = [e for e in await self.load_endpoints() if e.get("enabled", True)]
        results = await asyncio.gather(*(make_provider(e).check() for e in eps))
        statuses: dict[str, dict] = {}
        for e, status in zip(eps, results):
            statuses[e["id"]] = status
            self._status_cache[e["id"]] = status
        return statuses

    async def check_one(self, eid: str) -> dict:
        cfg = await self.get_endpoint(eid)
        if not cfg:
            raise LLMError("endpoint not found")
        status = await make_provider(cfg).check()
        self._status_cache[eid] = status
        return status

    async def models(self, eid: str) -> dict:
        cfg = await self.get_endpoint(eid)
        if not cfg:
            raise LLMError("endpoint not found")
        return await make_provider(cfg).models()

    def cached_status(self, eid: str) -> Optional[dict]:
        return self._status_cache.get(eid)

    async def snapshot(self) -> list[dict]:
        """Endpoints (masked) + cached status — for /api/llm and WebSocket."""
        out = []
        for e in await self.load_endpoints():
            pub = self.public_endpoint(e)
            pub["status"] = self._status_cache.get(
                e["id"], {"online": None, "error": None, "checked_at": None}
            )
            out.append(pub)
        return out


_manager: Optional[LLMManager] = None


def get_llm_manager() -> LLMManager:
    assert _manager is not None, "LLMManager not initialized"
    return _manager


def set_llm_manager(mgr: LLMManager) -> None:
    global _manager
    _manager = mgr
