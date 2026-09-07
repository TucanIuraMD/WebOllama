"""Tests for the Ollama HTTP API client (against mock transport)."""
import asyncio

import pytest

from webui.ollama_client import OllamaClient, OllamaError, parse_modelfile
from webui.mock import make_mock_ollama_client


@pytest.mark.asyncio
async def test_version():
    c = make_mock_ollama_client()
    v = await c.version()
    assert v["version"] == "0.32.6"


@pytest.mark.asyncio
async def test_tags_and_ps():
    c = make_mock_ollama_client()
    tags = await c.tags()
    assert len(tags) == 2
    # mock starts with an empty running list (matches a fresh Ollama start);
    # load a model, then /api/ps must reflect it
    await c.load("qwen3:8b")
    running = await c.running_models()
    assert running[0]["name"] == "qwen3:8b"
    assert running[0]["size_vram"] > 0


@pytest.mark.asyncio
async def test_show():
    c = make_mock_ollama_client()
    data = await c.show("qwen3:8b")
    assert data["model"] == "qwen3:8b"
    assert data["details"]["family"] == "qwen3"


@pytest.mark.asyncio
async def test_show_missing_raises_404():
    c = make_mock_ollama_client()
    with pytest.raises(OllamaError) as e:
        await c.show("nope:missing")
    assert e.value.code == 404


@pytest.mark.asyncio
async def test_copy_and_delete():
    c = make_mock_ollama_client()
    await c.copy("qwen3:8b", "qwen3:8b-copy")
    names = [m["name"] for m in await c.tags()]
    assert "qwen3:8b-copy" in names
    await c.delete("qwen3:8b-copy")
    names = [m["name"] for m in await c.tags()]
    assert "qwen3:8b-copy" not in names


@pytest.mark.asyncio
async def test_stop_unloads_via_keep_alive():
    c = make_mock_ollama_client()
    # stop on loaded model should not raise and actually unload
    await c.load("qwen3:8b")
    await c.stop("qwen3:8b")
    assert await c.running_models() == []


@pytest.mark.asyncio
async def test_pull_stream_progress():
    c = make_mock_ollama_client()
    events = []

    async def go():
        await c.pull("some:model", events.append)

    asyncio.get_running_loop().run_until_complete(go()) if False else await go()
    assert events, "expected progress events"
    assert any(e.status == "success" for e in events)
    assert max(e.percent for e in events) == 100.0


@pytest.mark.asyncio
async def test_status_reports_online():
    c = make_mock_ollama_client()
    await c.load("qwen3:8b")
    st = await c.status()
    assert st["online"] is True
    assert st["models_count"] == 2
    assert st["running_count"] == 1


def test_parse_modelfile_full():
    mf = """FROM qwen3:8b
PARAMETER temperature 0.7
PARAMETER num_ctx 8192
SYSTEM \"\"\"You are a helpful assistant.\"\"\"
LICENSE \"\"\"MIT\"\"\"
"""
    parsed = parse_modelfile(mf)
    assert parsed["from"] == "qwen3:8b"
    assert parsed["parameters"] == {"temperature": "0.7", "num_ctx": "8192"}
    assert "assistant" in parsed["system"]
    assert parsed["license"] == "MIT"


def test_parse_modelfile_messages():
    mf = 'MESSAGE user Hello\nMESSAGE assistant Hi!'
    parsed = parse_modelfile(mf)
    assert parsed["messages"] == [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi!"},
    ]
