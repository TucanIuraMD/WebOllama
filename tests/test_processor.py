"""PROCESSOR block — remote CPU/GPU stats of the Ollama server (mock only).

No real hardware is touched: the remote host-agent is replaced by an
httpx MockTransport (webui.mock.MockProcessorAgent), covering normal data,
multiple GPUs, missing GPU, HTTP failure and malformed payloads.
"""
import pytest

from webui import remote_sys
from webui.mock import MockProcessorAgent
from webui.remote_sys import RemoteProcessorCollector

GOOD_PAYLOAD = {
    "host": "192.168.80.22",
    "cpu": {"utilization": 42.0, "cores_physical": 8, "cores_logical": 16,
            "load": [3.2, 2.8, 2.4]},
    "gpus": [{
        "index": 0, "name": "Tesla V100-SXM2-16GB", "utilization": 74.0,
        "memory_used": 12_400_000_000, "memory_total": 16_160_000_000,
        "memory_utilization": 46.0, "temperature": 64,
    }],
    "timestamp": "2026-09-06T14:00:00Z",
}


def _login(client):
    r = client.post("/api/auth/login", json={"username": "admin", "password": "changeme"})
    assert r.status_code == 200


@pytest.fixture(autouse=True)
def _no_configured_url(monkeypatch):
    """Keep the real (unconfigured) PROCESSOR_URL out of unit tests."""
    monkeypatch.setattr(remote_sys, "PROCESSOR_URL", "")
    monkeypatch.setattr(remote_sys, "SYSINFO_URL", "")


def _collector(monkeypatch, agent: MockProcessorAgent) -> RemoteProcessorCollector:
    c = RemoteProcessorCollector(base_url="http://mock-agent")
    c._client = __import__("httpx").AsyncClient(
        transport=agent.transport(), base_url="http://mock-agent"
    )
    return c


@pytest.mark.asyncio
async def test_api_returns_processor_data(client, monkeypatch):
    _login(client)
    from starlette.testclient import TestClient as _  # noqa: F401  (app up)

    c = _collector(monkeypatch, MockProcessorAgent())
    monkeypatch.setattr(remote_sys, "_processor", c)
    r = client.get("/api/system/processor")
    assert r.status_code == 200
    data = r.json()
    assert data["available"] is True
    assert data["host"] == "192.168.80.22"
    assert data["source"] == "remote"
    assert data["timestamp"] == "2026-09-06T14:00:00Z"


@pytest.mark.asyncio
async def test_cpu_data_shape(client, monkeypatch):
    _login(client)
    c = _collector(monkeypatch, MockProcessorAgent())
    monkeypatch.setattr(remote_sys, "_processor", c)
    cpu = client.get("/api/system/processor").json()["cpu"]
    assert cpu["utilization"] == 42.0
    assert cpu["cores_physical"] == 8
    assert cpu["cores_logical"] == 16
    assert cpu["load"] == [3.2, 2.8, 2.4]


@pytest.mark.asyncio
async def test_gpu_data_shape(client, monkeypatch):
    _login(client)
    c = _collector(monkeypatch, MockProcessorAgent())
    monkeypatch.setattr(remote_sys, "_processor", c)
    g = client.get("/api/system/processor").json()["gpus"][0]
    assert g["name"] == "Tesla V100-SXM2-16GB"
    assert g["utilization"] == 74.0
    assert g["memory_used"] == 12_400_000_000
    assert g["memory_total"] == 16_160_000_000
    assert g["temperature"] == 64


@pytest.mark.asyncio
async def test_multiple_gpus(client, monkeypatch):
    _login(client)
    payload = dict(GOOD_PAYLOAD)
    payload["gpus"] = [
        {"index": 0, "name": "GPU Zero", "utilization": 10,
         "memory_used": 1_000_000_000, "memory_total": 16_000_000_000,
         "temperature": 55},
        {"index": 1, "name": "GPU One", "utilization": 90,
         "memory_used": 15_000_000_000, "memory_total": 16_000_000_000,
         "temperature": 71},
    ]
    c = _collector(monkeypatch, MockProcessorAgent(payload=payload))
    monkeypatch.setattr(remote_sys, "_processor", c)
    gpus = client.get("/api/system/processor").json()["gpus"]
    assert [g["index"] for g in gpus] == [0, 1]
    assert [g["name"] for g in gpus] == ["GPU Zero", "GPU One"]
    assert gpus[1]["utilization"] == 90.0


@pytest.mark.asyncio
async def test_gpu_unavailable_still_serves_cpu(client, monkeypatch):
    _login(client)
    payload = dict(GOOD_PAYLOAD)
    payload["gpus"] = []
    c = _collector(monkeypatch, MockProcessorAgent(payload=payload))
    monkeypatch.setattr(remote_sys, "_processor", c)
    data = client.get("/api/system/processor").json()
    assert data["available"] is True
    assert data["gpus"] == []
    assert data["cpu"]["utilization"] == 42.0


@pytest.mark.asyncio
async def test_remote_host_unreachable(client, monkeypatch):
    _login(client)
    import httpx as _httpx

    c = RemoteProcessorCollector(base_url="http://mock-agent")
    c._client = _httpx.AsyncClient(
        transport=_httpx.MockTransport(
            lambda request: (_ for _ in ()).throw(_httpx.ConnectError("refused"))
        ),
        base_url="http://mock-agent",
    )
    monkeypatch.setattr(remote_sys, "_processor", c)
    r = client.get("/api/system/processor")
    assert r.status_code == 200  # structured degradation, never 500
    data = r.json()
    assert data["available"] is False
    assert "unreachable" in data["reason"]
    assert data["gpus"] == []


@pytest.mark.asyncio
async def test_malformed_statistics(client, monkeypatch):
    _login(client)
    c = _collector(monkeypatch, MockProcessorAgent(payload={"host": 1, "cpu": "oops", "gpus": "nope"}))
    monkeypatch.setattr(remote_sys, "_processor", c)
    data = client.get("/api/system/processor").json()
    assert data["available"] is True        # payload shape is salvageable
    assert data["gpus"] == []               # bad gpus field -> empty, not crash
    assert data["cpu"]["utilization"] is None

    c2 = _collector(monkeypatch, MockProcessorAgent(payload=[1, 2, 3]))
    monkeypatch.setattr(remote_sys, "_processor", c2)
    data = client.get("/api/system/processor").json()
    assert data["available"] is False       # non-dict payload -> unavailable
    assert "malformed" in data["reason"]

    c3 = _collector(monkeypatch, MockProcessorAgent(payload={
        "cpu": {"utilization": "abc", "load": ["x", None]},
        "gpus": [{"name": "GPU", "utilization": None, "memory_used": "zzz"}],
    }))
    monkeypatch.setattr(remote_sys, "_processor", c3)
    data = client.get("/api/system/processor").json()
    g = data["gpus"][0]
    assert g["utilization"] is None and g["memory_used"] is None
    assert g["name"] == "GPU" and g["index"] == 0
    assert data["cpu"]["utilization"] is None


@pytest.mark.asyncio
async def test_processor_endpoint_requires_auth(client):
    # no login on purpose: unauthenticated request must be rejected
    assert client.get("/api/system/processor").status_code == 401


@pytest.mark.asyncio
async def test_not_configured_is_structured_not_error(client, monkeypatch):
    _login(client)
    monkeypatch.setattr(remote_sys, "_processor", RemoteProcessorCollector(base_url=""))
    data = client.get("/api/system/processor").json()
    assert data["available"] is False
    assert data["configured"] is False
    assert "not configured" in data["reason"]


@pytest.mark.asyncio
async def test_last_good_payload_served_on_transient_failure(client, monkeypatch):
    _login(client)
    import httpx as _httpx

    flaky = {"down": False}

    def handler(request):
        if flaky["down"]:
            raise _httpx.ConnectError("dropped")
        return _httpx.Response(200, json=GOOD_PAYLOAD, request=request)

    c = RemoteProcessorCollector(base_url="http://mock-agent")
    c._client = _httpx.AsyncClient(transport=_httpx.MockTransport(handler),
                                   base_url="http://mock-agent")
    monkeypatch.setattr(remote_sys, "_processor", c)
    first = await c.sample()
    assert first["available"] is True
    flaky["down"] = True
    second = await c.sample(force=True)
    assert second["available"] is False
    assert second["cpu"]["utilization"] == 42.0       # last good payload retained
    assert second["gpus"][0]["name"] == "Tesla V100-SXM2-16GB"
    assert second["stale"] is True and second["stale_age"] >= 0
