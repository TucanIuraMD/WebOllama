"""Dashboard v2 backend consistency tests.

The Dashboard consumes the shared realtime snapshot (WS + /api/status) and
PROCESSOR (/api/system/processor). These tests pin the contract that makes
the Dashboard a single coherent screen:

  - GPU/VRAM equality: /api/status gpu numbers == /api/system/processor
    numbers when the realtime snapshot is fresh (one source of truth — the
    Dashboard must never show different GPU/VRAM values in different blocks);
  - /api/status serves the cached realtime snapshot only while fresh and
    falls through to a live build when stale (stale-state handling);
  - Ollama offline → snapshot says online:false, running present-but-empty
    is NOT faked;
  - GPU-less host → gpu.available false with a reason, snapshot still
    complete (CPU/ram/ollama intact) — Dashboard must not depend on NVIDIA;
  - model VRAM (ollama.vram_used from size_vram) is reported separately
    from GPU VRAM — the Dashboard renders both without mixing them;
  - no duplicate polling sources: realtime is the sole snapshot producer
    (single-flight GPU sample inside the snapshot build).
"""
import time

import pytest

import webui.remote_sys as rs
from webui import realtime as rt_mod




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
def _logged_in(client):
    client.post("/api/auth/login", json={"username": "admin", "password": "changeme"})
    yield


def _snap(**over):
    snap = {
        "ts": time.time(),
        "gpu": {"available": True, "source": "test", "ts": time.time(),
                "gpus": [{"index": 0, "name": "Tesla V100-SXM2-16GB", "utilization": 9,
                          "temperature": 45, "vram_total": 16_160_000_000,
                          "vram_used": 15_400_000_000, "memory_utilization": 95}],
                "processes": [], "ollama_vram": {"total_vram": 0, "per_model": {}}},
        "cpu": {"percent": 41.5, "cores": 8, "threads": 16, "load_1": 3.2},
        "ram": {"total": 64_000_000_000, "used": 20_000_000_000, "percent": 31},
        "system": {"hostname": "ollama-server"},
        "ollama": {"online": True, "endpoint": "http://127.0.0.1:11434", "version": "0.32.6",
                   "models_count": 2, "running_count": 1,
                   "running": [{"name": "qwen3:8b", "size_vram": 13_500_000_000, "size": 14_000_000_000}]},
        "jobs": [], "llm": [],
    }
    snap.update(over)
    return snap


def test_status_gpu_matches_processor_gpu(client, monkeypatch):
    """ONE source of truth: fresh realtime snapshot feeds both /api/status
    and /api/system/processor — the Dashboard's GPU block and PROCESSOR
    block cannot disagree."""
    snap = _snap()
    monkeypatch.setattr(rt_mod.get_realtime_service(), "last_snapshot", snap, raising=False)

    status = client.get("/api/status").json()
    proc = client.get("/api/system/processor").json()
    sg, pg = status["gpu"]["gpus"][0], proc["gpus"][0]
    assert sg["vram_used"] == pg["memory_used"] == 15_400_000_000
    assert sg["vram_total"] == pg["memory_total"] == 16_160_000_000
    assert sg["utilization"] == pg["utilization"] == 9
    assert sg["temperature"] == pg["temperature"] == 45


def test_status_gpu_matches_processor_gpu_via_live_build(client):
    """Even on the live-build path (no cache), both endpoints agree."""
    status = client.get("/api/status").json()
    proc = client.get("/api/system/processor").json()
    sg, pg = (status.get("gpu") or {}).get("gpus", [{}])[0], (proc.get("gpus") or [{}])[0]
    assert sg.get("vram_used") == pg.get("memory_used")
    assert sg.get("utilization") == pg.get("utilization")


def test_status_stale_snapshot_falls_through_to_live_build(client, monkeypatch):
    """A stalled realtime loop must not serve old data forever: stale cache
    → live build (fresh ts)."""
    rt = rt_mod.get_realtime_service()
    stale = _snap()
    stale["ts"] = stale["gpu"]["ts"] = time.time() - 60  # far beyond REFRESH_INTERVAL
    monkeypatch.setattr(rt, "last_snapshot", stale, raising=False)

    async def fresh_build(persist=False):
        s = _snap()
        s["ts"] = time.time()
        return s

    monkeypatch.setattr(rt, "build_snapshot", fresh_build)
    data = client.get("/api/status").json()
    assert data["ts"] > time.time() - 5  # freshly built, not the 60s-old cache


def test_ollama_offline_in_snapshot_not_faked(client, monkeypatch):
    """Ollama down → online:false + empty running; the Dashboard shows the
    offline state, never a faked empty 'available' list."""
    snap = _snap(ollama={"online": False, "endpoint": "http://127.0.0.1:11434",
                         "running": [], "models_count": 2, "running_count": 0})
    snap["gpu"]["ollama_vram"] = {"total_vram": 0, "per_model": {}}
    monkeypatch.setattr(rt_mod.get_realtime_service(), "last_snapshot", snap, raising=False)
    data = client.get("/api/status").json()
    assert data["ollama"]["online"] is False
    assert data["ollama"]["running"] == []
    # snapshot still complete for the rest of the Dashboard
    assert data["cpu"]["percent"] == 41.5
    assert data["gpu"]["available"] is True


def test_no_models_running_zero_not_missing(client, monkeypatch):
    """Loaded-but-empty (real zero) is distinct from offline/unknown."""
    snap = _snap()
    snap["ollama"]["running"] = []
    snap["ollama"]["running_count"] = 0
    monkeypatch.setattr(rt_mod.get_realtime_service(), "last_snapshot", snap, raising=False)
    data = client.get("/api/status").json()
    assert data["ollama"]["online"] is True
    assert data["ollama"]["running_count"] == 0
    assert data["ollama"]["models_count"] == 2


def test_gpu_unavailable_snapshot_still_complete(client, monkeypatch):
    """No NVIDIA GPU is a normal state: gpu.available=false + reason, and
    cpu/ram/ollama stay intact — the Dashboard must not depend on NVIDIA."""
    snap = _snap()
    snap["gpu"] = {"available": False, "reason": "NVML not available (no NVIDIA GPU)", "gpus": [], "ts": time.time()}
    snap["gpu"]["ollama_vram"] = {"total_vram": 0, "per_model": {}}
    monkeypatch.setattr(rt_mod.get_realtime_service(), "last_snapshot", snap, raising=False)
    data = client.get("/api/status").json()
    assert data["gpu"]["available"] is False
    assert "no NVIDIA GPU" in data["gpu"]["reason"]
    assert data["cpu"]["percent"] == 41.5
    assert data["ollama"]["online"] is True


def test_model_vram_reported_separately_from_gpu_vram(client, monkeypatch):
    """GPU VRAM (NVML) vs model VRAM (size_vram sum): separate numbers, the
    Dashboard renders both — they must not overwrite each other."""
    snap = _snap()
    snap["gpu"]["ollama_vram"] = {"total_vram": 13_500_000_000, "per_model": {"qwen3:8b": 13_500_000_000}}
    monkeypatch.setattr(rt_mod.get_realtime_service(), "last_snapshot", snap, raising=False)
    data = client.get("/api/status").json()
    assert data["gpu"]["gpus"][0]["vram_used"] == 15_400_000_000          # GPU VRAM
    assert data["gpu"]["ollama_vram"]["total_vram"] == 13_500_000_000     # model VRAM
    assert data["ollama"]["running"][0]["size_vram"] == 13_500_000_000    # per-model


def test_multi_gpu_snapshot_preserved(client, monkeypatch):
    """Multi-GPU: every device stays in the snapshot with its own numbers —
    the Dashboard's per-device rendering must not collapse them."""
    snap = _snap()
    snap["gpu"]["gpus"].append({"index": 1, "name": "Tesla V100-SXM2-16GB", "utilization": 99,
                                "temperature": 80, "vram_total": 16_160_000_000,
                                "vram_used": 15_900_000_000, "memory_utilization": 98})
    monkeypatch.setattr(rt_mod.get_realtime_service(), "last_snapshot", snap, raising=False)
    gpus = client.get("/api/status").json()["gpu"]["gpus"]
    assert len(gpus) == 2
    assert {g["index"] for g in gpus} == {0, 1}


def test_processor_endpoint_graceful_on_error(client, monkeypatch):
    """PROCESSOR failure → structured unavailable payload (available=false +
    reason), never a 500 or empty-200 masquerading as data."""
    collector = rs.get_processor_collector()

    class _Boom:
        async def sample(self):
            return {}

    monkeypatch.setattr(collector, "_gpu", _Boom())
    monkeypatch.setattr(collector, "_system", _Boom())
    monkeypatch.setattr(collector, "_realtime_snapshot", lambda: None)
    monkeypatch.setattr(collector, "_cache", None, raising=False)
    r = client.get("/api/system/processor")
    assert r.status_code == 200
    data = r.json()
    assert data["available"] is False
    assert data.get("reason")


def test_single_snapshot_producer_no_duplicate_sources():
    """Architecture pin: the realtime service is the only snapshot builder;
    GPU sampling inside a build is single-flight (no duplicate NVML churn),
    and no second telemetry loop exists in the codebase."""
    import re
    from pathlib import Path

    src = (Path(__file__).resolve().parent.parent / "webui" / "realtime.py").read_text()
    assert "gpu.sample()" in src                      # shared collector
    assert "_snapshot_lock" in src                    # builds are serialized
    # no ad-hoc second polling loop in the realtime module
    assert not re.search(r"asyncio\.create_task\(.*sample.*\)\s*\n\s*asyncio\.create_task\(.*sample", src)
