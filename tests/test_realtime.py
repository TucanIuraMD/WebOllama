"""Regression tests for the realtime snapshot → metrics → history data path.

Guards the reported bug: Dashboard Power/GPU-utilization must reflect the real
NVML values flowing through build_snapshot, metric persistence and history.
"""
import asyncio
import json

import pytest

from webui.realtime import RealtimeService
from webui.db import Database


class _FakeWS:
    def __init__(self):
        self.sent = []

    async def broadcast(self, payload):
        self.sent.append(payload)


class _FakeGPU:
    """GPU collector stub returning changing power/utilization per call."""

    def __init__(self):
        self.calls = 0

    def _gpu(self):
        self.calls += 1
        return {
            "available": True,
            "source": "test",
            "gpus": [{
                "index": 0,
                "name": "Tesla V100-SXM2-16GB",
                "utilization": 78 + self.calls,
                "memory_utilization": 46,
                "temperature": 61,
                "vram_total": 17163003904,
                "vram_used": 12000000000,
                "vram_free": 5163003904,
                "power_draw": 150.5 + self.calls,
                "power_limit": 250.0,
                "fan_target": 63,
                "fan_pwm": 63,
                "fan_available": True,
                "clocks": 1530,
                "mem_clock": 877,
            }],
            "processes": [],
        }

    async def sample(self):
        return self._gpu()

    async def gpu_processes(self):
        return {"available": True, "processes": []}

    async def ollama_vram(self, running):
        return {"total_vram": 0, "per_model": {}}


class _FakeSystem:
    def __init__(self):
        self._proc = {"pid": 1, "name": "ollama", "cpu": 1.0, "memory": 0.5, "rss": 1000}

    async def sample(self):
        return {
            "hostname": "test", "os": "linux", "uptime": 10,
            "cpu": {"percent": 5, "load": (0.1, 0.2, 0.3)},
            "ram": {"total": 32e9, "used": 8e9, "percent": 25},
            "swap": {"total": 8e9, "used": 0, "percent": 0},
            "disk": {"total": 100e9, "used": 40e9, "percent": 40},
            "network": {"rx_rate": 1000, "tx_rate": 2000, "interfaces": {}},
            "processes": {"top_cpu": [], "top_memory": []},
        }

    async def ollama_process(self):
        return self._proc


class _FakeJobs:
    async def list(self, limit=30):
        return []


class _FakeOllama:
    async def status(self):
        return {"online": True, "version": "0.32.6", "models_count": 36,
                "running_count": 1, "running": [{"name": "qwen3:8b", "size_vram": 1e9}],
                "vram_used": 1e9, "error": None}


async def _make_service(db: Database):
    gpu = _FakeGPU()
    ws = _FakeWS()
    rt = RealtimeService(db, _FakeOllama(), gpu, _FakeSystem(), _FakeJobs(), ws.broadcast)
    return rt, gpu


@pytest.mark.asyncio
async def test_snapshot_contains_power_and_utilization(db):
    rt, _ = await _make_service(db)
    snap = await rt.build_snapshot()
    g = snap["gpu"]["gpus"][0]
    assert g["power_draw"] > 0
    assert g["utilization"] > 0
    assert snap["gpu"]["available"] is True


@pytest.mark.asyncio
async def test_snapshot_power_updates_between_calls(db):
    rt, gpu = await _make_service(db)
    s1 = await rt.build_snapshot()
    p1 = s1["gpu"]["gpus"][0]["power_draw"]
    s2 = await rt.build_snapshot()
    p2 = s2["gpu"]["gpus"][0]["power_draw"]
    assert p2 > p1  # fake collector increments power per call
    u1 = s1["gpu"]["gpus"][0]["utilization"]
    u2 = s2["gpu"]["gpus"][0]["utilization"]
    assert u2 > u1


@pytest.mark.asyncio
async def test_metrics_persisted_and_read_back(db):
    rt, gpu = await _make_service(db)
    # persist two snapshots with different power
    await rt.build_snapshot(persist=True)
    await rt.build_snapshot(persist=True)

    rows = await db.get_metrics("gpu", since=0)
    assert len(rows) >= 2
    powers = [r["data"]["gpus"][0]["power_draw"] for r in rows if r["data"]["gpus"]]
    utils = [r["data"]["gpus"][0]["utilization"] for r in rows if r["data"]["gpus"]]
    assert len(powers) >= 2
    assert powers[-1] > powers[0]  # later point has higher power
    assert utils[-1] > utils[0]


@pytest.mark.asyncio
async def test_history_payload_shape(db):
    rt, _ = await _make_service(db)
    await rt.build_snapshot(persist=True)
    rows = await db.get_metrics("gpu", since=0)
    row = rows[0]
    # shape expected by the frontend chart getter:
    #   p.data.gpus[0].power_draw / utilization
    assert "data" in row
    assert "gpus" in row["data"]
    assert "power_draw" in row["data"]["gpus"][0]
    assert "utilization" in row["data"]["gpus"][0]


@pytest.mark.asyncio
async def test_fan_fields_in_metrics(db):
    rt, _ = await _make_service(db)
    await rt.build_snapshot(persist=True)
    rows = await db.get_metrics("gpu", since=0)
    g = rows[0]["data"]["gpus"][0]
    assert "fan_target" in g
    assert "fan_pwm" in g


@pytest.mark.asyncio
async def test_snapshot_broadcast_contains_gpu(db):
    rt, _ = await _make_service(db)
    await rt.build_snapshot()
    # last_snapshot is exposed via /api/status
    snap = rt.last_snapshot
    assert "gpu" in snap
    assert snap["gpu"]["gpus"][0]["power_draw"] > 0
