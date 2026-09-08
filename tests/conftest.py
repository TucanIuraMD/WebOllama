"""Pytest fixtures — mock mode enabled for all tests (no real GPU/Ollama)."""
import asyncio
import os
import shutil
import tempfile
from pathlib import Path

import pytest
import pytest_asyncio

# Force test-friendly configuration BEFORE importing app modules
_TEST_ROOT = Path(tempfile.mkdtemp(prefix="webollama-test-"))
os.environ.setdefault("AUTH_ENABLED", "true")
os.environ.setdefault("DEFAULT_ADMIN_USER", "admin")
os.environ.setdefault("DEFAULT_ADMIN_PASSWORD", "changeme")
os.environ.setdefault("OLLAMA_URL", "http://mock")
os.environ.setdefault("WEBUI_PORT", "9099")
os.environ["DB_PATH"] = str(_TEST_ROOT / "app.db")
os.environ["LOG_FILE"] = str(_TEST_ROOT / "logs" / "test.log")
os.environ["METRICS_INTERVAL"] = "3600"  # don't spam DB during tests

import webui.mock as mock  # noqa: E402
import webui.ollama_client as oc  # noqa: E402
import webui.gpu_collector as gc  # noqa: E402


def _reset_singletons():
    """Reset module-level singletons between tests so state never leaks."""
    oc._client = None
    gc._collector = None
    import webui.jobs as jobs_mod
    import webui.audit as audit_mod
    import webui.realtime as realtime_mod
    import webui.deps as deps_mod
    import webui.ws_manager as ws_mod
    import webui.routers.console as console_mod
    import webui.db as db_mod

    jobs_mod._manager = None
    audit_mod._audit = None
    realtime_mod._service = None
    deps_mod._auth = None
    ws_mod._manager = None
    console_mod._console = None
    db_mod.Database._instance = None
    # PROCESSOR collector caches its payload (TTL 2s) — must reset or a
    # previous test's payload leaks into the next test's /api/system/processor
    import webui.remote_sys as remote_sys_mod
    remote_sys_mod._processor = None
    # ELECTRICITY service holds a Database handle — must not survive a reset
    import webui.electricity as electricity_mod
    electricity_mod._service = None


@pytest.fixture(autouse=True)
def _cleanup():
    _reset_singletons()
    yield
    _reset_singletons()


@pytest_asyncio.fixture
async def mock_ollama():
    client = mock.make_mock_ollama_client()
    oc._client = client
    return client


@pytest.fixture
def mock_gpu():
    g = mock.MockGPUCollector()
    gc._collector = g
    return g


@pytest_asyncio.fixture
async def db(tmp_path):
    from webui.db import Database

    d = Database(str(tmp_path / "test.db"))
    await d.connect()
    yield d
    await d.close()


@pytest_asyncio.fixture
async def auth_manager(db):
    from webui.auth import AuthManager

    return AuthManager(db)


@pytest_asyncio.fixture
async def job_manager(db, mock_ollama):
    from webui.jobs import JobManager, set_job_manager

    jm = JobManager(db, mock_ollama)
    set_job_manager(jm)
    await jm.start()
    return jm


@pytest.fixture
def client(mock_ollama, mock_gpu):
    """FastAPI TestClient with mock collectors installed."""
    from fastapi.testclient import TestClient

    import webui.main as main

    with TestClient(main.app) as tc:
        yield tc
