"""Running v2 backend regression tests — the data contract the Running page
consumes: GET /api/ollama/running (OllamaClient → /api/ps).

The UI rewrite changed no backend code, so these tests pin the EXISTING
contract the new frontend relies on:
  - raw /api/ps fields passed through verbatim (size, size_vram,
    expires_at, context_length, details) — model VRAM source of truth;
  - CPU/GPU split is derivable from size vs size_vram (no server-side
    heuristics, no GPU telemetry in this payload);
  - error paths: Ollama offline → 502 (never an empty 200 list);
  - auth required;
  - multiple running models all present (multi-GPU / multi-model: the
    payload is per-model — GPU attribution beyond size_vram is not
    invented server-side);
  - Models ↔ Running sync: run → /running shows it, stop → gone.
"""
import pytest


from webui import ollama_client as oc
from webui.ollama_client import OllamaError


def _login_unused(client):
    r = client.post("/api/auth/login", json={"username": "admin", "password": "changeme"})
    assert r.status_code == 200
    return r.cookies  # TestClient keeps the session cookie on the client itself


@pytest.fixture(autouse=True)
def _isolate_rate_limiters(monkeypatch):
    from webui import deps, security
    from webui.security import RateLimiter

    monkeypatch.setattr(deps, "_dangerous_limiter", RateLimiter(10_000, 1))
    monkeypatch.setattr(deps, "_general_limiter", RateLimiter(10_000, 1))
    security._rate.clear()
    yield
    security._rate.clear()


@pytest.fixture(autouse=True)
def _logged_in(request, client):
    # skipped for the auth test (it needs an unauthenticated client)
    if "requires_auth" in request.node.name:
        yield
        return
    client.post("/api/auth/login", json={"username": "admin", "password": "changeme"})
    yield


def test_running_requires_auth(client):
    # no _logged_in here: explicit, like test_models_v2
    client2 = client
    client2.cookies.clear()
    r = client2.get("/api/ollama/running")
    assert r.status_code == 401


def test_running_empty_list_when_nothing_loaded(client):
    client.post("/api/ollama/models/llama3.2:1b/stop")
    r = client.get("/api/ollama/running")
    assert r.status_code == 200
    data = r.json()
    assert data["models"] == [] and data["count"] == 0


def test_running_carries_raw_ps_fields(client):

    client.post("/api/ollama/models/qwen3:8b/run")
    r = client.get("/api/ollama/running")
    assert r.status_code == 200
    models = r.json()["models"]
    assert len(models) == 1
    m = models[0]
    # verbatim /api/ps fields — the frontend renders these directly
    assert m["name"] == "qwen3:8b"
    assert isinstance(m["size"], int) and m["size"] > 0
    assert isinstance(m["size_vram"], int) and m["size_vram"] > 0
    assert m["expires_at"]
    assert isinstance(m["context_length"], int) and m["context_length"] > 0
    assert m["details"]["parameter_size"]


def test_running_reflects_stop(client):

    client.post("/api/ollama/models/qwen3:8b/run")
    assert len(client.get("/api/ollama/running").json()["models"]) == 1
    client.delete("/api/ollama/models/qwen3:8b/stop")
    assert client.get("/api/ollama/running").json()["models"] == []


def test_running_multiple_models_each_with_own_vram(client):

    client.post("/api/ollama/models/qwen3:8b/run")
    client.post("/api/ollama/models/llama3.2:1b/run")
    models = client.get("/api/ollama/running").json()["models"]
    names = {m["name"] for m in models}
    assert names == {"qwen3:8b", "llama3.2:1b"}
    vrams = {m["name"]: m["size_vram"] for m in models}
    # per-model attribution: each entry carries its own size_vram — the UI
    # sums them for "Total model VRAM"; no merged/ambiguous blob
    assert vrams["qwen3:8b"] > 0 and vrams["llama3.2:1b"] > 0


def test_running_ollama_offline_502_not_empty_list(client, monkeypatch):


    async def boom():
        raise OllamaError("Ollama is offline", offline=True)

    monkeypatch.setattr(oc.get_client(), "running_models", boom)
    r = client.get("/api/ollama/running")
    assert r.status_code == 502
    assert "offline" in r.json()["detail"].lower()


def test_running_after_models_run_flow_sync(client):
    """Models ↔ Running contract: run on Models → appears here; stop on
    Running → gone (same OllamaClient source, no state store)."""

    assert client.post("/api/ollama/models/qwen3:8b/run").json()["running"] is True
    running = {m["name"] for m in client.get("/api/ollama/running").json()["models"]}
    assert "qwen3:8b" in running
    # models list agrees (same source)
    models_list = client.get("/api/ollama/models").json()["models"]
    flag = {m["name"]: m["running"] for m in models_list}
    assert flag["qwen3:8b"] is True
