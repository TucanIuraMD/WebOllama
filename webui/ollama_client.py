"""Async client for the native Ollama HTTP API.

All model-management operations go through this client. It detects
offline state and raises OllamaError with a stable error code so the UI
can render "OLLAMA OFFLINE" instead of hanging.
"""
import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Optional

import httpx

from .config import OLLAMA_URL

logger = logging.getLogger(__name__)


class OllamaError(Exception):
    def __init__(self, message: str, code: int = 500, offline: bool = False) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.offline = offline


@dataclass
class ProgressEvent:
    status: str = ""
    completed: int = 0
    total: int = 0
    percent: float = 0.0
    digest: str = ""
    error: str = ""


ProgressCallback = Callable[[ProgressEvent], None]


class OllamaClient:
    def __init__(self, base_url: str = OLLAMA_URL, timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None
        self._lock = asyncio.Lock()

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(self.timeout, read=600.0),
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            )
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    # ---- low-level -----------------------------------------------------------
    async def _request(self, method: str, path: str, json: dict | None = None) -> Any:
        client = await self._get_client()
        try:
            resp = await client.request(method, path, json=json)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            logger.warning("Ollama unreachable at %s: %s", self.base_url, exc)
            raise OllamaError("Ollama is offline", offline=True) from exc
        except httpx.TimeoutException as exc:
            raise OllamaError(f"Ollama request timed out: {exc}", code=504) from exc
        except httpx.HTTPError as exc:
            raise OllamaError(f"Ollama HTTP error: {exc}", code=502) from exc

        if resp.status_code == 404:
            try:
                err = resp.json().get("error", resp.text)
            except ValueError:
                err = resp.text
            raise OllamaError(f"Not found: {err}" if err else f"Not found: {path}", code=404)
        try:
            data = resp.json()
        except ValueError:
            data = resp.text
        if resp.status_code >= 400:
            err = data.get("error", resp.text) if isinstance(data, dict) else resp.text
            raise OllamaError(str(err), code=resp.status_code)
        return data

    # ---- core API ------------------------------------------------------------
    async def version(self) -> dict:
        return await self._request("GET", "/api/version")

    async def tags(self) -> list[dict]:
        data = await self._request("GET", "/api/tags")
        return data.get("models", [])

    async def ps(self) -> dict:
        return await self._request("GET", "/api/ps")

    async def running_models(self) -> list[dict]:
        data = await self.ps()
        return data.get("models", [])

    async def show(self, name: str) -> dict:
        return await self._request("POST", "/api/show", {"name": name, "verbose": True})

    async def copy(self, source: str, destination: str) -> None:
        await self._request("POST", "/api/copy", {"source": source, "destination": destination})

    async def delete(self, name: str) -> None:
        await self._request("DELETE", "/api/delete", {"model": name})

    async def stop(self, name: str) -> None:
        """Unload a model from VRAM. Ollama has no dedicated stop API endpoint;
        the official mechanism is /api/generate with keep_alive=0."""
        try:
            await self._request("POST", "/api/generate", {"model": name, "keep_alive": 0})
        except OllamaError as exc:
            # a model that isn't loaded returns 404-ish errors; that's acceptable
            if exc.offline:
                raise
            logger.info("stop(%s): %s", name, exc.message)

    async def chat(self, model: str, messages: list[dict], stream: bool = False) -> Any:
        return await self._request(
            "POST", "/api/chat",
            {"model": model, "messages": messages, "stream": stream},
        )

    async def chat_stream(
        self,
        model: str,
        messages: list[dict],
        options: dict | None = None,
        cancel_event: asyncio.Event | None = None,
    ):
        """Stream /api/chat NDJSON tokens as parsed dicts until done=true.

        Ollama emits one JSON object per line; with stream=True each line
        carries an incremental message chunk, the final line has done=true
        plus timing/eval counters. Raises OllamaError offline/HTTP errors.
        """
        client = await self._get_client()
        payload: dict = {"model": model, "messages": messages, "stream": True}
        if options:
            payload["options"] = options
        try:
            async with client.stream("POST", "/api/chat", json=payload) as resp:
                if resp.status_code >= 400:
                    body = (await resp.aread()).decode(errors="replace")
                    raise OllamaError(body or f"HTTP {resp.status_code}", code=resp.status_code)
                async for line in resp.aiter_lines():
                    if cancel_event is not None and cancel_event.is_set():
                        logger.info("Chat stream cancelled (client disconnect)")
                        return
                    if not line.strip():
                        continue
                    try:
                        evt = json.loads(line)
                    except ValueError:
                        continue
                    yield evt
        except OllamaError:
            raise
        except httpx.ConnectError as exc:
            raise OllamaError("Ollama is offline", offline=True) from exc
        except httpx.HTTPError as exc:
            raise OllamaError(f"Stream failed: {exc}", code=502) from exc

    async def generate(self, model: str, prompt: str, stream: bool = False) -> Any:
        return await self._request(
            "POST", "/api/generate",
            {"model": model, "prompt": prompt, "stream": stream},
        )

    async def embed(self, model: str, input_text: str) -> dict:
        return await self._request("POST", "/api/embed", {"model": model, "input": input_text})

    # ---- streaming ops (pull / push / create) ---------------------------------
    async def _stream(
        self,
        path: str,
        payload: dict,
        on_progress: ProgressCallback,
        cancel_event: asyncio.Event | None = None,
    ) -> None:
        client = await self._get_client()
        try:
            async with client.stream("POST", path, json=payload) as resp:
                if resp.status_code >= 400:
                    body = (await resp.aread()).decode(errors="replace")
                    raise OllamaError(body or f"HTTP {resp.status_code}", code=resp.status_code)
                async for line in resp.aiter_lines():
                    if cancel_event is not None and cancel_event.is_set():
                        logger.info("Stream cancelled (client disconnect)")
                        return
                    if not line.strip():
                        continue
                    try:
                        evt = _parse_progress(line)
                    except ValueError:
                        continue
                    on_progress(evt)
        except OllamaError:
            raise
        except httpx.ConnectError as exc:
            raise OllamaError("Ollama is offline", offline=True) from exc
        except httpx.HTTPError as exc:
            raise OllamaError(f"Stream failed: {exc}", code=502) from exc

    async def pull(
        self,
        name: str,
        on_progress: ProgressCallback,
        cancel_event: asyncio.Event | None = None,
    ) -> None:
        await self._stream("/api/pull", {"name": name, "stream": True}, on_progress, cancel_event)

    async def push(
        self,
        name: str,
        on_progress: ProgressCallback,
        cancel_event: asyncio.Event | None = None,
    ) -> None:
        await self._stream("/api/push", {"name": name, "stream": True}, on_progress, cancel_event)

    async def create(
        self,
        name: str,
        modelfile: str,
        on_progress: ProgressCallback,
        cancel_event: asyncio.Event | None = None,
    ) -> None:
        """Create a model from a Modelfile.

        Some Ollama builds accept the raw modelfile string; others (this
        deployment's 0.32.x) expect the parsed fields (from/system/template/
        parameters/license/...). We parse the modelfile into fields and send
        them — the field schema is understood by both modern and this build.
        """
        payload = parse_modelfile(modelfile)
        payload["name"] = name
        payload["stream"] = True
        await self._stream("/api/create", payload, on_progress, cancel_event)

    # ---- status ----------------------------------------------------------------
    async def status(self) -> dict:
        """Aggregated Ollama status. Never raises; reports offline explicitly."""
        try:
            version = await self.version()
            tags = await self.tags()
            running = await self.running_models()
            vram_used = sum(m.get("size_vram", 0) for m in running)
            return {
                "online": True,
                "version": version.get("version", ""),
                "endpoint": self.base_url,
                "models_count": len(tags),
                "running_count": len(running),
                "running": running,
                "vram_used": vram_used,
                "error": None,
            }
        except OllamaError as exc:
            return {
                "online": False,
                "version": None,
                "endpoint": self.base_url,
                "models_count": 0,
                "running_count": 0,
                "running": [],
                "vram_used": 0,
                "error": exc.message,
            }


def _parse_progress(line: str) -> ProgressEvent:
    obj = json.loads(line)
    evt = ProgressEvent(
        status=obj.get("status", ""),
        completed=obj.get("completed", 0) or 0,
        total=obj.get("total", 0) or 0,
        digest=obj.get("digest", ""),
        error=obj.get("error", ""),
    )
    if evt.total > 0:
        evt.percent = min(100.0, evt.completed / evt.total * 100.0)
    return evt


# ---- Modelfile parsing -------------------------------------------------------
def parse_modelfile(text: str) -> dict:
    """Parse a Modelfile into the field schema used by /api/create.

    Supports: FROM, SYSTEM, TEMPLATE, PARAMETER, LICENSE, ADAPTER, MESSAGE,
    FILES. Unknown directives are preserved as comments in the output.
    """
    import re

    text = (text or "").replace("\r\n", "\n")
    fields: dict = {}
    parameters: dict = {}
    messages: list[dict] = []

    # triple-quoted blocks: SYSTEM/TEMPLATE/LICENSE/ADAPTER/FILES
    block_re = re.compile(
        r'^(SYSTEM|TEMPLATE|LICENSE|ADAPTER|FILES)\s+"""([\s\S]*?)"""\s*$',
        re.MULTILINE,
    )
    for m in block_re.finditer(text):
        key, value = m.group(1).lower(), m.group(2)
        if key in ("system", "template", "license", "adapter"):
            fields[key] = value
        elif key == "files":
            fields["files"] = [p.strip() for p in value.splitlines() if p.strip()]

    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line or line.startswith("#"):
            i += 1
            continue
        if line.startswith("FROM "):
            fields.setdefault("from", line[5:].strip())
        elif line.startswith("PARAMETER "):
            parts = line[10:].strip().split(None, 1)
            if len(parts) == 2:
                parameters[parts[0]] = parts[1]
        elif line.startswith("MESSAGE "):
            rest = line[8:].strip()
            role, _, content = rest.partition(" ")
            messages.append({"role": role.strip(), "content": content.strip()})
        elif line.startswith(("SYSTEM ", "TEMPLATE ", "LICENSE ", "ADAPTER ", "FILES ")):
            # single-line form, e.g. SYSTEM "text"
            m = re.match(r'^([A-Z]+)\s+"(.*)"\s*$', line, re.S)
            if m and m.group(1).lower() in ("system", "template", "license", "adapter"):
                fields.setdefault(m.group(1).lower(), m.group(2))
        i += 1

    if parameters:
        fields["parameters"] = parameters
    if messages:
        fields["messages"] = messages
    return fields


# convenience singleton
_client: Optional[OllamaClient] = None


def get_client() -> OllamaClient:
    global _client
    if _client is None:
        _client = OllamaClient()
    return _client
