"""Agents tab — data layer, API and matrix behaviour (mock Ollama, no network).

Notes on isolation:
- The TestClient lifespan DB (see conftest) is a single file per pytest
  session, so these tests use unique model names and relative (delta)
  assertions instead of absolute counts.
- The process-wide rate limiters are swapped for high-capacity instances
  for the duration of this module and restored afterwards, so these tests
  neither hit 429 themselves nor exhaust the budget of other test modules.
"""
import uuid

import pytest

from webui.ollama_client import OllamaError


@pytest.fixture(autouse=True)
def _isolate_rate_limiters(monkeypatch):
    from webui import deps, security
    from webui.security import RateLimiter

    monkeypatch.setattr(deps, "_dangerous_limiter", RateLimiter(10_000, 1))
    monkeypatch.setattr(deps, "_general_limiter", RateLimiter(10_000, 1))
    # RateLimiter keeps events in a process-wide dict shared by every
    # instance; wipe it so these tests neither inherit nor leak budget.
    security._rate.clear()
    yield
    security._rate.clear()


def _login(client):
    r = client.post("/api/auth/login", json={"username": "admin", "password": "changeme"})
    assert r.status_code == 200
    return r.cookies


def _save(client, **payload):
    return client.post("/api/agents/assessments", json=payload)


def _uname(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


@pytest.mark.asyncio
async def test_agents_config_seeded(client):
    _login(client)
    r = client.get("/api/agents")
    assert r.status_code == 200
    data = r.json()
    slugs = [a["slug"] for a in data["agents"]]
    for expected in ("hermes", "opencode", "claude", "openwebui"):
        assert expected in slugs
    cap_slugs = [c["slug"] for c in data["capabilities"]]
    for expected in ("coding", "chat", "analysis", "vision", "data", "rag", "tools", "research"):
        assert expected in cap_slugs


@pytest.mark.asyncio
async def test_capabilities_endpoint(client):
    _login(client)
    r = client.get("/api/agents/capabilities")
    assert r.status_code == 200
    slugs = [c["slug"] for c in r.json()["capabilities"]]
    for expected in ("coding", "chat", "analysis", "vision", "data", "rag", "tools", "research"):
        assert expected in slugs


@pytest.mark.asyncio
async def test_matrix_lists_ollama_models_untested_by_default(client):
    _login(client)
    r = client.get("/api/agents/matrix")
    assert r.status_code == 200
    data = r.json()
    assert data["ollama_online"] is True
    names = [m["name"] for m in data["models"]]
    assert "qwen3:8b" in names and "llama3.2:1b" in names
    # a model nobody assessed has no assessment rows: it shows as untested,
    # never as failed
    assert not any(a["model"] == "llama3.2:1b" for a in data["assessments"])


@pytest.mark.asyncio
async def test_assessment_crud_and_matrix_update(client):
    _login(client)
    agents = client.get("/api/agents").json()["agents"]
    opencode = next(a for a in agents if a["slug"] == "opencode")
    model = _uname("crud-probe")

    before = client.get("/api/agents/matrix").json()["counts"]["assessments"]

    # create
    r = _save(client, model=model, agent_id=opencode["id"], status="good",
              note="ok with tools", capabilities=["coding", "tools"])
    assert r.status_code == 200
    a = r.json()["assessment"]
    assert a["status"] == "good"
    assert a["agent_slug"] == "opencode"
    assert sorted(c["slug"] for c in a["capabilities"]) == ["coding", "tools"]
    assert a["tested_at"] is not None  # set automatically

    # matrix reflects it
    m = client.get("/api/agents/matrix").json()
    assert m["counts"]["assessments"] == before + 1
    assert m["counts"]["tested"] >= 1

    # update overwrites (upsert by model+agent)
    r = _save(client, model=model, agent_id=opencode["id"], status="works", note="")
    assert r.status_code == 200
    m = client.get("/api/agents/matrix").json()
    assert m["counts"]["assessments"] == before + 1
    row = next(x for x in m["assessments"] if x["model"] == model)
    assert row["status"] == "works"
    assert row["capabilities"] == []  # capabilities replaced on save

    # delete -> cell back to untested
    r = client.delete(f"/api/agents/assessments/{row['id']}")
    assert r.status_code == 200
    r = client.delete(f"/api/agents/assessments/{row['id']}")
    assert r.status_code == 404
    m = client.get("/api/agents/matrix").json()
    assert m["counts"]["assessments"] == before
    assert not any(x["model"] == model for x in m["assessments"])


@pytest.mark.asyncio
async def test_untested_is_not_failed_and_clears_tested_at(client):
    _login(client)
    agents = client.get("/api/agents").json()["agents"]
    hermes = next(a for a in agents if a["slug"] == "hermes")
    model = _uname("untested-probe")
    _save(client, model=model, agent_id=hermes["id"], status="good")
    r = _save(client, model=model, agent_id=hermes["id"], status="untested")
    a = r.json()["assessment"]
    assert a["status"] == "untested"
    assert a["tested_at"] is None  # untested is not a test result


@pytest.mark.asyncio
async def test_model_variants_stay_separate(client):
    _login(client)
    agents = client.get("/api/agents").json()["agents"]
    opencode = next(a for a in agents if a["slug"] == "opencode")
    base = _uname("deepseek-variant")
    _save(client, model=base, agent_id=opencode["id"], status="works")
    _save(client, model=f"{base}-tools-8k", agent_id=opencode["id"], status="failed")
    _save(client, model=f"{base}-tools-16k", agent_id=opencode["id"], status="good")
    rows = client.get(f"/api/agents/models/{base}-tools-16k").json()["assessments"]
    assert len(rows) == 1 and rows[0]["status"] == "good"
    m = client.get("/api/agents/matrix").json()
    models = {a["model"]: a["status"] for a in m["assessments"] if a["model"].startswith(base)}
    assert models == {
        base: "works",
        f"{base}-tools-8k": "failed",
        f"{base}-tools-16k": "good",
    }


@pytest.mark.asyncio
async def test_assessment_validation(client):
    _login(client)
    agents = client.get("/api/agents").json()["agents"]
    aid = agents[0]["id"]
    assert _save(client, model="x", agent_id=aid, status="excellent").status_code == 400
    assert _save(client, model="x", agent_id=aid, status="").status_code == 400
    assert _save(client, model="x", agent_id=99999, status="works").status_code == 400
    assert _save(client, model="", agent_id=aid, status="works").status_code == 400
    assert _save(client, model="x", agent_id=aid, status="works",
                 capabilities=["nonexistent-cap"]).status_code == 400
    assert _save(client, model="x", agent_id=aid, status="works",
                 capabilities="coding").status_code == 400


@pytest.mark.asyncio
async def test_new_agent_and_capability_extensible(client):
    _login(client)
    r = client.post("/api/agents/agents", json={"name": _uname("Automation")})
    assert r.status_code == 200
    slug = r.json()["agent"]["slug"]
    assert slug.startswith("automation-")
    r = client.post("/api/agents/capabilities", json={"name": _uname("OCR")})
    assert r.status_code == 200
    assert r.json()["capability"]["slug"].startswith("ocr-")
    m = client.get("/api/agents/matrix").json()
    assert any(a["slug"] == slug for a in m["agents"])


@pytest.mark.asyncio
async def test_agent_endpoints_require_auth(client):
    assert client.get("/api/agents/matrix").status_code == 401
    assert client.get("/api/agents").status_code == 401


@pytest.mark.asyncio
async def test_matrix_without_ollama_keeps_assessments(client, mock_ollama):
    """Ollama offline must not break the knowledge base."""
    _login(client)
    agents = client.get("/api/agents").json()["agents"]
    aid = agents[0]["id"]
    model = _uname("offline-model")
    _save(client, model=model, agent_id=aid, status="works")

    async def dead_request(*a, **k):
        raise OllamaError("Ollama is offline", offline=True)

    mock_ollama._request = dead_request
    m = client.get("/api/agents/matrix").json()
    assert m["ollama_online"] is False
    assert m["models"] == []
    rows = [a for a in m["assessments"] if a["model"] == model]
    assert len(rows) == 1 and rows[0]["status"] == "works"
