"""Tests for the GPU collector."""
import pytest

from webui.mock import MockGPUCollector


@pytest.mark.asyncio
async def test_mock_gpu_snapshot():
    g = MockGPUCollector()
    data = await g.sample()
    assert data["available"] is True
    gpu = data["gpus"][0]
    assert gpu["name"] == "Tesla V100-SXM2-16GB"
    assert gpu["vram_total"] == 16_160_000_000  # 16 GB
    assert gpu["utilization"] == 78
    assert gpu["temperature"] == 61
    assert gpu["power_draw"] == 185.0


@pytest.mark.asyncio
async def test_gpu_processes_include_ollama():
    g = MockGPUCollector()
    procs = await g.gpu_processes()
    assert procs["available"] is True
    names = [p["name"] for p in procs["processes"]]
    assert "ollama" in names


@pytest.mark.asyncio
async def test_ollama_vram_attribution():
    g = MockGPUCollector()
    running = [{"name": "qwen3:8b", "size_vram": 4_600_000_000}]
    v = await g.ollama_vram(running)
    assert v["total_vram"] == 4_600_000_000
    assert v["per_model"]["qwen3:8b"] == 4_600_000_000
