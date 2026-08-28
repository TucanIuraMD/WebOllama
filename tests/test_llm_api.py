"""Tests for the LLM API provider and REST endpoints."""
import asyncio
import json

import pytest
import httpx
from fastapi.testclient import TestClient

from webui.llm_api import (
    LLMManager,
    mask_api_key,
    validate_endpoint_url,
    LLMError,
    get_llm_manager,
    set_llm_manager,
    make_provider,
    DEFAULT_ENDPOINTS,
)


# --------------------------------------------------------------------------- #
#  Provider tests (against mock transport — no real network)
# --------------------------------------------------------------------------- #
def _mock_transport(responses: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path in responses:
            return responses[path]
        return httpx.Response(404, json={"error": "not found"})
    return httpx.MockTransport(handler)


def _make_provider(cfg=None, transport=None):
    cfg = cfg or {"id": "test", "name": "Test", "base_url": "http://mock", "type": "openai-compatible"}
    p = make_provider(cfg)
    async def _get(path, timeout):
        tr = transport or _mock_transport({})
        async with httpx.AsyncClient(timeout=timeout, transport=tr) as client:
            return await client.get(p.base_url + path)
    p._get = _get
    return p


@pytest.mark.asyncio
async def test_provider_check_online():
    resp_data = {"object": "list", "data": [{"id": "model1"}, {"id": "model2"}]}
    transport = _mock_transport({"/models": httpx.Response(200, json=resp_data)})
    p = _make_provider(transport=transport)
    status = await p.check()
    assert status["online"] is True
    assert status["models_count"] == 2
    assert status["latency_ms"] is not None
    assert status["http_status"] == 200


@pytest.mark.asyncio
async def test_provider_check_offline():
    transport = _mock_transport({})
    p = _make_provider(transport=transport)
    status = await p.check()
    assert status["online"] is False
    assert status["http_status"] == 404
    assert status["error"] is not None


@pytest.mark.asyncio
async def test_provider_check_connect_error():
    p = _make_provider()
    async def _get(path, timeout):
        raise httpx.ConnectError("connection refused")
    p._get = _get
    status = await p.check()
    assert status["online"] is False
    assert "connection failed" in (status["error"] or "")


@pytest.mark.asyncio
async def test_provider_models():
    resp_data = {"object": "list", "data": [{"id": "m1", "object": "model", "created": 1000, "owned_by": "test"}]}
    transport = _mock_transport({"/models": httpx.Response(200, json=resp_data)})
    p = _make_provider(transport=transport)
    result = await p.models()
    assert result["ok"] is True
    assert len(result["models"]) == 1
    assert result["models"][0]["id"] == "m1"


@pytest.mark.asyncio
async def test_provider_models_preserves_extra_fields():
    resp_data = {"object": "list", "data": [{
        "id": "m1", "object": "model", "created": 1000, "owned_by": "test",
        "context_length": 1048576, "capabilities": {"tool_calling": True},
    }]}
    transport = _mock_transport({"/models": httpx.Response(200, json=resp_data)})
    p = _make_provider(transport=transport)
    result = await p.models()
    m = result["models"][0]
    assert m["context_length"] == 1048576
    assert m["capabilities"]["tool_calling"] is True


@pytest.mark.asyncio
async def test_provider_auth_header():
    cfg = {"id": "test", "name": "Test", "base_url": "http://mock", "type": "openai-compatible", "api_key": "sk-test123"}
    p = _make_provider(cfg)
    h = p._headers()
    assert h["Authorization"] == "Bearer sk-test123"


# --------------------------------------------------------------------------- #
#  API key masking
# --------------------------------------------------------------------------- #
def test_mask_api_key():
    assert mask_api_key("") == ""
    assert mask_api_key("sk") == "**"
    assert mask_api_key("sk-1234567890") == "sk-****7890"
    assert mask_api_key("sk-proj-abcdefghijklmnop") == "sk-****mnop"


# --------------------------------------------------------------------------- #
#  URL validation (SSRF protection)
# --------------------------------------------------------------------------- #
def test_validate_endpoint_url():
    assert validate_endpoint_url("http://example.com/v1") == "http://example.com/v1"
    assert validate_endpoint_url("https://api.openai.com/v1") == "https://api.openai.com/v1"
    with pytest.raises(LLMError):
        validate_endpoint_url("")
    with pytest.raises(LLMError):
        validate_endpoint_url("ftp://evil.com")
    with pytest.raises(LLMError):
        validate_endpoint_url("javascript:alert(1)")
    with pytest.raises(LLMError):
        validate_endpoint_url("http://")


# --------------------------------------------------------------------------- #
#  Manager
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_manager_default_endpoints(db):
    mgr = LLMManager(db)
    eps = await mgr.load_endpoints()
    assert len(eps) == 2
    assert eps[0]["id"] == "ollama"
    assert eps[1]["id"] == "omnirouter"
    assert "base_url" in eps[0]
    # api_key defaults to empty in DEFAULT_ENDPOINTS
    assert eps[0].get("api_key", "") == ""


@pytest.mark.asyncio
async def test_manager_public_endpoint_masks_key(db):
    mgr = LLMManager(db)
    cfg = {"id": "test", "name": "Test", "base_url": "http://x.com", "api_key": "sk-secret123", "enabled": True}
    pub = mgr.public_endpoint(cfg)
    assert "sk-secret123" not in pub["api_key"]
    assert pub["api_key"] == "sk-****t123"


@pytest.mark.asyncio
async def test_manager_save_and_load(db):
    mgr = LLMManager(db)
    eps = await mgr.load_endpoints()
    eps.append({"id": "custom", "name": "Custom", "type": "openai-compatible", "base_url": "http://c.com", "api_key": "key", "enabled": True})
    await mgr.save_endpoints(eps)
    loaded = await mgr.load_endpoints()
    assert len(loaded) == 3
    assert loaded[2]["id"] == "custom"
    assert loaded[2]["base_url"] == "http://c.com"


@pytest.mark.asyncio
async def test_manager_get_endpoint(db):
    mgr = LLMManager(db)
    ep = await mgr.get_endpoint("ollama")
    assert ep is not None
    assert ep["name"] == "Ollama"
    assert await mgr.get_endpoint("nonexistent") is None


@pytest.mark.asyncio
async def test_manager_check_all_caches(db):
    mgr = LLMManager(db)
    # check_all performs real checks; in an offline test env the endpoints
    # report offline, in the live env they report online — just verify the
    # shape and that results are cached without raising.
    statuses = await mgr.check_all()
    assert isinstance(statuses, dict)
    assert "ollama" in statuses
    assert "omnirouter" in statuses
    assert statuses["ollama"]["online"] in (True, False)
    # cached
    cached = mgr.cached_status("ollama")
    assert cached is not None
    assert cached["online"] in (True, False)