"""Regression tests: stale GPU snapshot must never surface to API/WS/history.

Models the reported incident on the remote GPU host (192.168.80.22):
  nvidia-smi (real):   util=0,  temp=42, power=40
  WebOllama showed:    util=75, temp=55, power=218.99
i.e. the UI displayed an OLD GPU snapshot. These tests pin down correct
behavior of every consumer of the GPU snapshot:

  1. GPUCollector.sample() — single-flight cache; a completed collection
     must replace the cached one and be returned (no stale window);
  2. RealtimeService.build_snapshot() — a hung Ollama (600s read timeout)
     must NOT stall GPU/System collection (hard SLA on status refresh);
  3. History persistence — rows are stamped with the snapshot's OWN ts
     (metric read time), not the persist moment;
  4. /api/status — last_snapshot served only while fresh.
"""
import asyncio
import time

import pytest

from webui.realtime import RealtimeService, REFRESH_INTERVAL


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
def _snap(util: int, temp: int, power: float, ts: float | None = None) -> dict:
    """Build a GPU snapshot like GPUCollector would produce."""
    ts = ts if ts is not None else time.time()
    return {
        "available": True,
        "source": "test",
        "ts": ts,
        "collected_at": ts,
        "gpus": [{
            "index": 0,
            "name": "Tesla V100-SXM2-16GB",
            "utilization": util,
            "temperature": temp,
            "power_draw": power,
            "vram_total": 16_160_000_000,
            "vram_used": 8_500_000_000,
        }],
        "processes": [],
    }


SNAPSHOT_A = dict(util=75, temp=55, power=218.99)   # what WebOllama showed (stale)
SNAPSHOT_B = dict(util=0, temp=42, power=40.0)      # what nvidia-smi showed (real)


class ScriptedGPU:
    """GPU collector stub returning a scripted sequence of snapshots."""

    def __init__(self, *snapshots):
        self._seq = [s if "ts" in s else _snap(**s) for s in snapshots]
        self._last = self._seq[-1]
        self.calls = 0
        self.delay = 0.0

    async def sample(self):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self._seq:
            self._last = self._seq.pop(0)
        return self._last

    async def gpu_processes(self):
        return {"available": True, "processes": []}

    async def ollama_vram(self, running):
        return {"total_vram": 0, "per_model": {}}


class FakeOllama:
    def __init__(self, status=None, delay=0.0):
        self._status = status
        self.delay = delay
        self.calls = 0

    async def status(self):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if isinstance(self._status, Exception):
            raise self._status
        return self._status or {
            "online": True, "version": "0.32.6", "endpoint": "http://mock",
            "models_count": 2, "running_count": 0, "running": [],
            "vram_used": 0, "error": None,
        }


class FakeSystem:
    async def sample(self):
        return {"cpu": {"percent": 12.5, "load": (0, 0, 0)},
                "ram": {"used": 1, "total": 2}, "swap": {},
                "disk": {}, "network": {}, "hostname": "t", "os": "t", "uptime": 1}

    async def ollama_process(self):
        return None


class FakeJobs:
    async def list(self, limit=30):
        return []


class FakeDB:
    def __init__(self):
        self.inserted = []

    async def insert_metric(self, ts, kind, payload):
        self.inserted.append((ts, kind, payload))

    async def prune_old_metrics(self):
        pass


class FakeWS:
    def __init__(self):
        self.sent = []

    async def broadcast(self, payload):
        self.sent.append(payload)


def _make_service(gpu, ollama, db=None):
    return RealtimeService(
        db=db or FakeDB(),
        client=ollama,
        gpu=gpu,
        system=FakeSystem(),
        jobs=FakeJobs(),
        ws_broadcast=FakeWS().broadcast,
    )


# --------------------------------------------------------------------------- #
# 1. Collector cache: completed collection replaces cached one
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_collector_returns_snapshot_b_after_b_arrives():
    """After snapshot B is collected, sample() must return B — never A."""
    from webui.gpu_collector import GPUCollector

    c = GPUCollector(sysinfo_url="")
    c._cache = _snap(**SNAPSHOT_A)          # old value currently cached
    c._last = time.monotonic() - 999.0      # cache already expired

    # Simulate the completed collection of B (what _collect() would fetch)
    async def fake_collect():
        return _snap(**SNAPSHOT_B)

    c._collect = fake_collect
    out = await c.sample()
    assert out["gpus"][0]["utilization"] == SNAPSHOT_B["util"]
    assert out["gpus"][0]["temperature"] == SNAPSHOT_B["temp"]
    assert out["gpus"][0]["power_draw"] == SNAPSHOT_B["power"]


@pytest.mark.asyncio
async def test_collector_concurrent_sample_share_one_collection():
    """Parallel sample() calls must share ONE collection (single-flight)."""
    from webui.gpu_collector import GPUCollector

    c = GPUCollector(sysinfo_url="")
    collect_calls = 0

    async def slow_collect():
        nonlocal collect_calls
        collect_calls += 1
        await asyncio.sleep(0.05)
        return _snap(**SNAPSHOT_B)

    c._collect = slow_collect
    results = await asyncio.gather(*(c.sample() for _ in range(5)))
    assert collect_calls == 1
    assert all(r["gpus"][0]["utilization"] == 0 for r in results)
    # and every caller gets the same dict object (no diverged copies)
    assert all(r is results[0] for r in results)


@pytest.mark.asyncio
async def test_snapshot_carries_timestamps():
    """Every snapshot must carry ts + collected_at so staleness is detectable."""
    from webui.gpu_collector import GPUCollector

    c = GPUCollector(sysinfo_url="")
    c._collect = async_fn_returning(_snap(**SNAPSHOT_B))
    out = await c.sample()
    assert out.get("ts") and out.get("collected_at")
    assert out["gpus"][0].get("ts")


def async_fn_returning(value):
    async def _fn():
        return value
    return _fn


# --------------------------------------------------------------------------- #
# 2. Realtime loop resilience: hung Ollama must not freeze GPU data
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_hung_ollama_does_not_stall_snapshot():
    """Ollama hanging far beyond the realtime interval must still yield a
    fresh GPU snapshot (SLA 3s), not a 600s-frozen loop."""
    gpu = ScriptedGPU(SNAPSHOT_A, SNAPSHOT_B)
    hung = FakeOllama(delay=30.0)  # >> _OLLAMA_SLA (3s), < 600s read timeout
    svc = _make_service(gpu, hung)

    # First snapshot (A) may consume the Ollama SLA; second must be fast & fresh.
    await asyncio.wait_for(svc.build_snapshot(), timeout=6.0)
    started = time.monotonic()
    snap = await asyncio.wait_for(svc.build_snapshot(), timeout=6.0)
    elapsed = time.monotonic() - started

    assert elapsed < REFRESH_INTERVAL + 5.0, (
        f"build_snapshot blocked {elapsed:.1f}s on hung Ollama"
    )
    assert snap["gpu"]["gpus"][0]["power_draw"] == SNAPSHOT_B["power"]
    assert snap["gpu"]["gpus"][0]["utilization"] == 0


@pytest.mark.asyncio
async def test_ollama_exception_reports_offline_not_crash():
    """Ollama raising must degrade to online=false, snapshot still builds."""
    gpu = ScriptedGPU(SNAPSHOT_B)
    boom = FakeOllama(status=RuntimeError("connection refused"))
    svc = _make_service(gpu, boom)

    snap = await svc.build_snapshot()
    assert snap["ollama"]["online"] is False
    assert snap["gpu"]["gpus"][0]["temperature"] == SNAPSHOT_B["temp"]


# --------------------------------------------------------------------------- #
# 3. History: stamped with the snapshot's own ts
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_history_records_snapshot_ts_not_persist_time():
    """If GPU data came from an earlier read, history must carry THAT ts."""
    old_ts = time.time() - 4.0  # snapshot read 4s ago (e.g. cache hit path)
    gpu = ScriptedGPU(_snap(**SNAPSHOT_B, ts=old_ts))
    db = FakeDB()
    svc = _make_service(gpu, FakeOllama(), db=db)

    await svc.build_snapshot(persist=True)

    gpu_rows = [e for e in db.inserted if e[1] == "gpu"]
    assert gpu_rows, "gpu metric must be persisted"
    ts, _, payload = gpu_rows[0]
    assert abs(ts - old_ts) < 0.5, (
        f"history ts {ts} must be the snapshot read time {old_ts}, not persist time"
    )
    g = payload["gpus"][0]
    assert g["utilization"] == 0 and g["temperature"] == 42 and g["power_draw"] == 40.0


# --------------------------------------------------------------------------- #
# 4. /api/status: last_snapshot served only while fresh
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_api_status_rejects_stale_last_snapshot(monkeypatch):
    """A last_snapshot older than the refresh interval must not be served."""
    import webui.routers.status as status_mod
    from webui.realtime import get_realtime_service

    gpu = ScriptedGPU(SNAPSHOT_B)
    svc = _make_service(gpu, FakeOllama())
    svc.last_snapshot = {"ts": time.time() - (REFRESH_INTERVAL + 5.0), "gpu": _snap(**SNAPSHOT_A)}

    async def fake_build(persist=False):
        return {"ts": time.time(), "gpu": _snap(**SNAPSHOT_B)}

    monkeypatch.setattr(svc, "build_snapshot", fake_build)

    class _FakeRT:
        pass

    monkeypatch.setattr(
        status_mod, "get_realtime_service", lambda: svc
    )
    out = await status_mod.status(user={"username": "t"})
    assert out["gpu"]["gpus"][0]["utilization"] == 0  # fresh B, not stale A


# --------------------------------------------------------------------------- #
# 5. End-to-end through the mock app: /api/gpu reflects collector updates
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_api_gpu_reflects_latest_collection(client, mock_gpu):
    """Scripted A → B: /api/gpu must return B after B is collected."""
    def _login(c):
        r = c.post("/api/auth/login", json={"username": "admin", "password": "changeme"})
        assert r.status_code == 200
        return r.cookies

    _login(client)
    _snapA = _snap(**SNAPSHOT_A)
    _snapB = _snap(**SNAPSHOT_B)
    mock_gpu.data = _snapA
    first = client.get("/api/gpu")
    assert first.status_code == 200
    assert first.json()["gpus"][0]["power_draw"] == 218.99

    mock_gpu.data = _snapB  # new NVML read completed
    second = client.get("/api/gpu")
    assert second.status_code == 200
    body = second.json()
    assert body["gpus"][0]["utilization"] == 0
    assert body["gpus"][0]["temperature"] == 42
    assert body["gpus"][0]["power_draw"] == 40.0
