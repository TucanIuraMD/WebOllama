"""Tests for the system collector (psutil — real, no mocks)."""
import pytest

from webui.sys_collector import SystemCollector


@pytest.mark.asyncio
async def test_system_sample_shape():
    s = SystemCollector()
    data = await s.sample()
    assert "cpu" in data and "ram" in data and "disk" in data and "network" in data
    assert data["cpu"]["percent"] >= 0
    assert data["ram"]["total"] > 0
    assert data["disk"]["total"] > 0
    assert data["uptime"] > 0
    assert "hostname" in data
