"""Integration tests for REST API endpoints via TestClient (mock mode)."""
import pytest


@pytest.fixture(autouse=True)
def _isolate_rate_limiters(monkeypatch):
    """test_api touches dangerous-limited endpoints (chat/run, models run);
    isolate the shared process-wide limiter so tests don't 429 each other."""
    from webui import deps, security
    from webui.security import RateLimiter

    monkeypatch.setattr(deps, "_dangerous_limiter", RateLimiter(10_000, 1))
    monkeypatch.setattr(deps, "_general_limiter", RateLimiter(10_000, 1))
    security._rate.clear()
    yield
    security._rate.clear()


def _login(client):
    """Helper: login and return session cookies."""
    r = client.post("/api/auth/login", json={"username": "admin", "password": "changeme"})
    assert r.status_code == 200
    return r.cookies


@pytest.mark.asyncio
async def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ok"
    assert data["ollama_online"] is True


@pytest.mark.asyncio
async def test_models_list(client):
    _login(client)
    r = client.get("/api/ollama/models")
    assert r.status_code == 200
    data = r.json()
    assert data["count"] == 2
    names = [m["name"] for m in data["models"]]
    assert "qwen3:8b" in names


@pytest.mark.asyncio
async def test_running_models(client):
    _login(client)
    # mock starts with nothing loaded (fresh Ollama start); load one first
    assert client.post("/api/ollama/models/qwen3:8b/run").status_code == 200
    r = client.get("/api/ollama/running")
    assert r.status_code == 200
    data = r.json()
    assert data["count"] >= 1


@pytest.mark.asyncio
async def test_show_model(client):
    _login(client)
    r = client.post("/api/ollama/models/qwen3:8b/show")
    assert r.status_code == 200
    data = r.json()
    assert data["details"]["family"] == "qwen3"


@pytest.mark.asyncio
async def test_copy_delete(client):
    _login(client)
    r = client.post("/api/ollama/models/qwen3:8b/copy", json={"destination": "qwen3:test-copy"})
    assert r.status_code == 200
    # verify
    models = client.get("/api/ollama/models").json()["models"]
    assert any("qwen3:test-copy" in m["name"] for m in models)
    # delete
    r = client.delete("/api/ollama/models/qwen3:test-copy")
    assert r.status_code == 200
    models = client.get("/api/ollama/models").json()["models"]
    assert not any("qwen3:test-copy" in m["name"] for m in models)


@pytest.mark.asyncio
async def test_stop_model(client):
    _login(client)
    r = client.delete("/api/ollama/models/qwen3:8b/stop")
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_pull_creates_job(client):
    _login(client)
    r = client.post("/api/ollama/models/pull", json={"name": "some:model"})
    assert r.status_code == 200
    data = r.json()
    assert "job_id" in data
    # check job exists
    r2 = client.get(f"/api/jobs/{data['job_id']}")
    assert r2.status_code == 200
    assert r2.json()["status"] in ("running", "complete")


@pytest.mark.asyncio
async def test_create_model(client):
    _login(client)
    modelfile = "FROM qwen3:8b\nPARAMETER temperature 0.2\nSYSTEM \"\"\"test\"\"\""
    r = client.post("/api/ollama/models/create", json={"name": "new-test:1", "modelfile": modelfile})
    assert r.status_code == 200
    data = r.json()
    assert "job_id" in data


@pytest.mark.asyncio
async def test_system_endpoint(client):
    _login(client)
    r = client.get("/api/system")
    assert r.status_code == 200
    data = r.json()
    assert "cpu" in data
    assert "ram" in data


@pytest.mark.asyncio
async def test_gpu_endpoint(client, mock_gpu):
    _login(client)
    r = client.get("/api/gpu")
    assert r.status_code == 200
    data = r.json()
    assert data["available"] is True
    assert data["gpus"][0]["name"] == "Tesla V100-SXM2-16GB"


@pytest.mark.asyncio
async def test_console_run(client, mock_ollama):
    _login(client)
    r = client.post("/api/console/run", json={"command": "ollama version"})
    assert r.status_code == 200
    data = r.json()
    assert data["output"] == "0.32.6"
    assert data["exit_code"] == 0


@pytest.mark.asyncio
async def test_console_blocks_injection(client):
    _login(client)
    r = client.post("/api/console/run", json={"command": "ollama rm $(rm -rf /)"})
    assert r.status_code == 200  # the endpoint itself returns 200 with error
    assert r.json()["exit_code"] == 2
    assert r.json()["error"]


@pytest.mark.asyncio
async def test_chat_run(client, mock_ollama):
    _login(client)
    r = client.post("/api/chat/run", json={
        "model": "qwen3:8b",
        "messages": [{"role": "user", "content": "hello"}],
    })
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["message"]["role"] == "assistant"
    assert data["message"]["content"] == "mock chat reply"
    assert data["model"] == "qwen3:8b"
    assert data["eval_count"] == 3


@pytest.mark.asyncio
async def test_chat_run_validation(client):
    _login(client)
    r = client.post("/api/chat/run", json={"messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 400
    r = client.post("/api/chat/run", json={"model": "qwen3:8b", "messages": []})
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_settings(client):
    _login(client)
    r = client.get("/api/settings")
    assert r.status_code == 200
    data = r.json()
    assert "ollama_url" in data
    assert "auth_enabled" in data


@pytest.mark.asyncio
async def test_audit_log(client):
    _login(client)
    # trigger some auditable actions
    client.post("/api/console/run", json={"command": "ollama version"})
    client.get("/api/audit")
    # admin can view audit
    r = client.get("/api/audit?limit=10")
    assert r.status_code == 200
    rows = r.json()["rows"]
    assert any(r["action"] == "login" for r in rows)


@pytest.mark.asyncio
async def test_jobs_list(client):
    _login(client)
    r = client.get("/api/jobs")
    assert r.status_code == 200
    data = r.json()
    assert "jobs" in data


@pytest.mark.asyncio
async def test_logs_webui(client):
    _login(client)
    r = client.get("/api/logs/webui?tail=10")
    assert r.status_code == 200
    assert "lines" in r.json()


@pytest.mark.asyncio
async def test_static_files(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "WebOllama" in r.text
    r2 = client.get("/css/app.css")
    assert r2.status_code == 200
    assert "--bg" in r2.text