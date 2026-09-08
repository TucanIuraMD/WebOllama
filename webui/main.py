"""WebOllama — FastAPI application entry point."""
import asyncio
import logging
import logging.handlers
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import BASE_DIR, LOG_FILE, LOG_LEVEL, WEBUI_HOST, WEBUI_PORT
from .db import Database
from .jobs import JobManager
from .ollama_client import OllamaClient, get_client
from .realtime import RealtimeService
from .routers import (
    agents as agents_router,
    audit as audit_router,
    auth as auth_router,
    chat as chat_router,
    console as console_router,
    electricity as electricity_router,
    jobs as jobs_router,
    llm as llm_router,
    logs as logs_router,
    ollama as ollama_router,
    processor as processor_router,
    settings as settings_router,
    status as status_router,
    ws as ws_router,
)
from .ws_manager import get_ws_manager

logger = logging.getLogger(__name__)


def setup_logging() -> None:
    root = logging.getLogger()
    root.setLevel(LOG_LEVEL)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    root.addHandler(stream)

    log_path = Path(LOG_FILE)
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_path, maxBytes=5 * 1024 * 1024, backupCount=3
        )
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)
    except Exception as exc:
        print(f"WARNING: cannot create log file {LOG_FILE}: {exc}", file=sys.stderr)


# ---- globals shared across lifespan --------------------------------------------
db: Database
client: OllamaClient
job_manager: JobManager
realtime: RealtimeService


@asynccontextmanager
async def lifespan(app: FastAPI):
    global db, client, job_manager, realtime
    setup_logging()

    db = Database()
    await db.connect()

    from .auth import AuthManager

    auth = AuthManager(db)
    await auth.ensure_default_admin()
    await db.delete_expired_sessions()

    client = get_client()
    job_manager = JobManager(db, client)
    from .jobs import set_job_manager

    set_job_manager(job_manager)
    await job_manager.start()

    ws = get_ws_manager()
    job_manager.set_broadcast(ws.broadcast)
    console_router.set_console_job_hook(
        lambda op, name, modelfile=None: (
            job_manager.submit(op, name, (lambda j: job_manager.run_pull(j, name))) if op == "pull"
            else job_manager.submit(op, name, (lambda j: job_manager.run_push(j, name))) if op == "push"
            else job_manager.submit(op, name, (lambda j: job_manager.run_create(j, name, modelfile or "FROM " + name)))
        )
    )

    from .gpu_collector import get_gpu_collector
    from .sys_collector import get_system_collector
    from .realtime import set_realtime_service
    from .llm_api import LLMManager, set_llm_manager

    llm_mgr = LLMManager(db)
    set_llm_manager(llm_mgr)
    await llm_mgr.start()

    realtime = RealtimeService(db, client, get_gpu_collector(), get_system_collector(), job_manager, ws.broadcast)
    set_realtime_service(realtime)
    await realtime.start()

    logger.info("WebOllama started — Ollama: %s", client.base_url)
    yield

    await realtime.stop()
    await llm_mgr.stop()
    await client.close()
    await db.close()
    logger.info("WebOllama stopped")


app = FastAPI(title="WebOllama", version="1.0.0", lifespan=lifespan)

# CORS: allow same-origin and optional local dev; cookies not shared cross-origin
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost", "http://127.0.0.1"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

STATIC_DIR = Path(__file__).resolve().parent / "static"


@app.get("/api/health")
async def health():
    rt = realtime.last_snapshot if "realtime" in globals() and realtime else {}
    ollama = rt.get("ollama", {}) if rt else {}
    if not ollama:
        ollama = await client.status()
    return {
        "status": "ok",
        "ollama_online": ollama.get("online"),
        "version": "1.0.0",
    }


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(STATIC_DIR / "index.html")


app.include_router(auth_router.router)
app.include_router(status_router.router)
app.include_router(processor_router.router)
app.include_router(ollama_router.router)
app.include_router(agents_router.router)
app.include_router(chat_router.router)
app.include_router(jobs_router.router)
app.include_router(console_router.router)
app.include_router(electricity_router.router)
app.include_router(llm_router.router)
app.include_router(logs_router.router)
app.include_router(settings_router.router)
app.include_router(audit_router.router)
app.include_router(ws_router.router)


# catch-all for SPA routes — MUST be declared after all API routers
@app.get("/{full_path:path}", include_in_schema=False)
async def spa(full_path: str):
    candidate = STATIC_DIR / full_path
    if candidate.is_file():
        return FileResponse(candidate)
    # fall back to index.html for client-side routing
    index_file = STATIC_DIR / "index.html"
    if index_file.is_file():
        return FileResponse(index_file)
    return JSONResponse({"error": "not found"}, status_code=404)


def run() -> None:
    import uvicorn

    uvicorn.run("webui.main:app", host=WEBUI_HOST, port=WEBUI_PORT, reload=False)


if __name__ == "__main__":
    run()
