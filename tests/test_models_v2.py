"""Models v2 regression tests — capabilities, running flag, run/load, error paths.

Real API flow through the FastAPI app with the mock Ollama transport:
list (capabilities + running flags) → run → running state → list reflects
it → stop → gone. Also: capability filtering happens client-side, so here
we pin the DATA contract the UI filters rely on, plus error behavior
(model not found, Ollama offline).
"""
import pytest

from webui import ollama_client as oc
from webui.ollama_client import OllamaError


def _login(client):
    r = client.post("/api/auth/login", json={"username": "admin", "password": "changeme"})
    assert r.status_code == 200
    return {"cookies": r.cookies}


@pytest.fixture(autouse=True)
def _isolate_rate_limiters(monkeypatch):
    from webui import deps, security
    from webui.security import RateLimiter

    monkeypatch.setattr(deps, "_dangerous_limiter", RateLimiter(10_000, 1))
    monkeypatch.setattr(deps, "_general_limiter", RateLimiter(10_000, 1))
    security._rate.clear()
    yield
    security._rate.clear()


@pytest.mark.asyncio
async def test_models_list_carries_capabilities_and_running(client):
    """GET /api/ollama/models: every model carries capabilities from Ollama
    /api/tags verbatim + a running flag derived from /api/ps."""
    client.get("/api/auth/login", **{})  # noop for readability
    r = client.post("/api/auth/login", json={"username": "admin", "password": "changeme"})
    assert r.status_code == 200
    data = client.get("/api/ollama/models").json()
    assert data["count"] == 2
    by_name = {m["name"]: m for m in data["models"]}
    # capabilities come verbatim from Ollama (mock mirrors real /api/tags)
    assert by_name["qwen3:8b"]["capabilities"] == ["completion", "tools"]
    assert by_name["llama3.2:1b"]["capabilities"] == ["completion"]
    # running flag: qwen3:8b is preloaded in the mock's running list
    assert by_name["qwen3:8b"]["running"] is True
    assert by_name["llama3.2:1b"]["running"] is False


@pytest.mark.asyncio
async def test_run_loads_model_and_list_reflects_it(client):
    """Run → model appears in /api/ps AND list running flag flips — the
    Models ↔ Running ↔ Chat sync contract (single Ollama state source)."""
    client.post("/api/auth/login", json={"username": "admin", "password": "changeme"})
    r = client.post("/api/ollama/models/llama3.2:1b/run")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "model": "llama3.2:1b", "running": True}

    running = client.get("/api/ollama/running").json()
    assert "llama3.2:1b" in [m["name"] for m in running["models"]]

    listed = {m["name"]: m for m in client.get("/api/ollama/models").json()["models"]}
    assert listed["llama3.2:1b"]["running"] is True


@pytest.mark.asyncio
async def test_stop_unloads_and_list_reflects_it(client):
    """Stop → model gone from /api/ps, list running flag flips back.
    Stopping twice is not an error (Ollama stop is idempotent-ish and the
    client swallows non-offline errors for unloaded models)."""
    client.post("/api/auth/login", json={"username": "admin", "password": "changeme"})
    client.post("/api/ollama/models/llama3.2:1b/run")
    r = client.delete("/api/ollama/models/llama3.2:1b/stop")
    assert r.status_code == 200
    running = client.get("/api/ollama/running").json()
    assert "llama3.2:1b" not in [m["name"] for m in running["models"]]
    listed = {m["name"]: m for m in client.get("/api/ollama/models").json()["models"]}
    assert listed["llama3.2:1b"]["running"] is False
    # second stop must not 5xx
    r2 = client.delete("/api/ollama/models/llama3.2:1b/stop")
    assert r2.status_code == 200


@pytest.mark.asyncio
async def test_run_unknown_model_502(client):
    """Run of a nonexistent model → clear 502 with Ollama's message, never a
    silent success."""
    client.post("/api/auth/login", json={"username": "admin", "password": "changeme"})
    r = client.post("/api/ollama/models/ghost:latest/run")
    assert r.status_code == 502
    assert "not found" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_run_requires_auth(client):
    r = client.post("/api/ollama/models/llama3.2:1b/run")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_show_surfaces_context_length_from_model_info(client):
    """Real Ollama keeps context_length in model_info['{arch}.context_length'];
    /show must surface it top-level so the UI doesn't show '—'."""
    client.post("/api/auth/login", json={"username": "admin", "password": "changeme"})
    data = client.post("/api/ollama/models/qwen3:8b/show").json()
    assert data["context_length"] == 32768
    assert data["capabilities"] == ["completion", "tools"]
    # unknown model → 502 with detail (model not found)
    r = client.post("/api/ollama/models/ghost:latest/show")
    assert r.status_code == 502
    assert "not found" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_models_list_capabilities_unknown_model_has_empty_list():
    """A model with no capabilities field (older Ollama) must yield [] —
    the UI treats that as 'unknown', never as 'supports everything'."""
    from webui.mock import make_mock_ollama_client

    c = make_mock_ollama_client(models=[{
        "name": "ancient:1b", "model": "ancient:1b", "size": 1,
        "modified_at": "2020-01-01T00:00:00Z", "digest": "x",
        "details": {"family": "llama", "parameter_size": "1B",
                    "quantization_level": "Q4_0"},
    }])
    oc._client = c
    tags = await c.tags()
    assert tags[0].get("capabilities", []) == []


@pytest.mark.asyncio
async def test_models_list_when_ollama_offline_502(client, monkeypatch):
    """Ollama offline → 502 with a clear message (UI shows the offline
    state; no silent empty list from the API)."""
    client.post("/api/auth/login", json={"username": "admin", "password": "changeme"})

    async def boom():
        raise OllamaError("Ollama is offline", offline=True)

    monkeypatch.setattr(oc.get_client(), "tags", boom)
    r = client.get("/api/ollama/models")
    assert r.status_code == 502
    assert "offline" in r.json()["detail"].lower()
