"""WebSocket hub — one /ws connection receives periodic full snapshots plus
targeted updates (job updates, model changes, console output)."""
import asyncio
import json
import logging
from typing import Any, Awaitable, Callable, Optional, Set

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class WSManager:
    def __init__(self) -> None:
        self._connections: Set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._connections.add(ws)

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            self._connections.discard(ws)

    async def broadcast(self, payload: dict) -> None:
        if not self._connections:
            return
        message = json.dumps(payload, default=str)
        async with self._lock:
            conns = list(self._connections)
        for ws in conns:
            try:
                await ws.send_text(message)
            except Exception:
                async with self._lock:
                    self._connections.discard(ws)

    @property
    def client_count(self) -> int:
        return len(self._connections)


_manager: Optional[WSManager] = None


def get_ws_manager() -> WSManager:
    global _manager
    if _manager is None:
        _manager = WSManager()
    return _manager
