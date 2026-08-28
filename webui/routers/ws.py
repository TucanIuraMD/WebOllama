"""WebSocket endpoint for real-time updates."""
import logging
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..ws_manager import get_ws_manager

logger = logging.getLogger(__name__)
router = APIRouter()


@router.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    mgr = get_ws_manager()
    await mgr.connect(ws)
    logger.info("WebSocket client connected (%d total)", mgr.client_count)
    try:
        while True:
            msg = await ws.receive_text()
            # clients can send ping to keep alive
            if msg == "ping":
                await ws.send_text('{"type":"pong"}')
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.debug("WebSocket error: %s", exc)
    finally:
        await mgr.disconnect(ws)
        logger.info("WebSocket client disconnected (%d remaining)", mgr.client_count)