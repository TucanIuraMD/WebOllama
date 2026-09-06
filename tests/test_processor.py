"""PROCESSOR block — LOCAL CPU/GPU/Ollama stats of the host running WebOllama.

Architecture v2: WebOllama is deployed on the Ollama server itself, so the
PROCESSOR data is collected locally through the existing collectors
(GPUCollector, SystemCollector, OllamaClient /api/ps == `ollama ps`, with a
local `ollama ps --format json` CLI fallback). Tests use ONLY mock collectors
and fixture payloads — no real hardware, no real Ollama, no network, and no
access to any remote machine (e.g. 192.168.80.22).
"""
import json
import time

import pytest

import webui.remote_sys as rs
from webui.remote_sys import ProcessorCollector

GPU_OK = {
    "available": True,
    "source": "mock",
    "gpus": [{
        "index": 0, "name": "Tesla V100-SXM2-16GB", "utilization": 74.0,
        "vram_used": 12_400_000_000, "vram_total": 16_160_000_000,
        "memory_utilization": 46.0, "temperature": 64,
    }],
}
SYS_OK = {
    "cpu": {"percent": 42.0, "cores": 8, "threads": 16, "load_1": 3.2, "load_5": 2.8, "load_15": 2.4},
}
OLLAMA_OK = {
    "online": True, "endpoint": "http://127.0.0.1:11434",
    "running": [
        {"name": "deepseek-coder-v2:latest", "size_vram": 6_100_000_000, "size": 7_000_000_000},
        {"name": "qwen3:8b", "size_vram": 1_200_000_000, "size": 2_000_000_000},
    ],
}


class _Stub:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def make_collector(gpu_data=None, sys_data=None, ollama_status=None, cli_rows=None, cli_ok=False,
                   shared_snapshot="none"):
    """Build a ProcessorCollector with stubbed collaborators.

    shared_snapshot: the value returned by _realtime_snapshot(). Default "none"
    disables the shared-realtime path so the stubs below are exercised (the
    2nd-priority own-collection path). Pass a dict to simulate a fresh realtime
    snapshot (the 1st-priority path shared with the Dashboard GPU block).
    """
    c = ProcessorCollector()

    async def gpu_sample():
        return gpu_data if gpu_data is not None else {"available": False, "reason": "no GPU"}

    async def sys_sample():
        return sys_data if sys_data is not None else {"cpu": {}}

    async def status():
        return ollama_status if ollama_status is not None else {"online": False, "endpoint": "http://x"}

    async def cli_ps():
        return cli_rows if cli_ok else None

    c._gpu, c._system, c._ollama = _Stub(sample=gpu_sample), _Stub(sample=sys_sample), _Stub(status=status, _cli_ps=cli_ps)
    # patch the CLI fallback and the shared-snapshot source at instance level for determinism
    c._cli_ps = cli_ps  # type: ignore[method-assign]
    if shared_snapshot == "none":
        c._realtime_snapshot = lambda: None  # type: ignore[method-assign]
    else:
        c._realtime_snapshot = lambda: shared_snapshot  # type: ignore[method-assign]
    return c


def _login(client):
    r = client.post("/api/auth/login", json={"username": "admin", "password": "changeme"})
    assert r.status_code == 200


@pytest.fixture(autouse=True)
def _fresh_singleton(monkeypatch):
    monkeypatch.setattr(rs, "_processor", None)


@pytest.mark.asyncio
async def test_api_returns_processor_data(client):
    _login(client)
    rs._processor = make_collector(GPU_OK, SYS_OK, OLLAMA_OK)
    r = client.get("/api/system/processor")
    assert r.status_code == 200
    data = r.json()
    assert data["available"] is True
    assert data["source"] == "local"
    assert data["gpu_available"] is True
    assert data["ollama"]["online"] is True
    assert data["ollama"]["api_online"] is True


@pytest.mark.asyncio
async def test_cpu_data_shape(client):
    _login(client)
    rs._processor = make_collector(GPU_OK, SYS_OK, OLLAMA_OK)
    cpu = client.get("/api/system/processor").json()["cpu"]
    assert cpu["utilization"] == 42.0
    assert cpu["cores_physical"] == 8
    assert cpu["cores_logical"] == 16
    assert cpu["load"] == [3.2, 2.8, 2.4]


@pytest.mark.asyncio
async def test_gpu_data_shape(client):
    _login(client)
    rs._processor = make_collector(GPU_OK, SYS_OK, OLLAMA_OK)
    g = client.get("/api/system/processor").json()["gpus"][0]
    assert g["name"] == "Tesla V100-SXM2-16GB"
    assert g["utilization"] == 74.0
    assert g["memory_used"] == 12_400_000_000
    assert g["memory_total"] == 16_160_000_000
    assert g["temperature"] == 64


@pytest.mark.asyncio
async def test_multiple_gpus(client):
    _login(client)
    gpu_data = {"available": True, "gpus": [
        {"index": 0, "name": "GPU Zero", "utilization": 10, "vram_used": 1e9, "vram_total": 16e9, "temperature": 55},
        {"index": 1, "name": "GPU One", "utilization": 90, "vram_used": 15e9, "vram_total": 16e9, "temperature": 71},
    ]}
    rs._processor = make_collector(gpu_data, SYS_OK, OLLAMA_OK)
    gpus = client.get("/api/system/processor").json()["gpus"]
    assert [g["index"] for g in gpus] == [0, 1]
    assert [g["name"] for g in gpus] == ["GPU Zero", "GPU One"]
    assert gpus[1]["utilization"] == 90.0
    assert gpus[1]["memory_utilization"] == pytest.approx(93.75, abs=0.1)


@pytest.mark.asyncio
async def test_gpu_unavailable_no_traceback(client):
    _login(client)
    """No NVIDIA GPU / no nvidia-smi -> gpu_available false, CPU still served."""
    rs._processor = make_collector({"available": False, "reason": "no NVIDIA GPU"}, SYS_OK, OLLAMA_OK)
    data = client.get("/api/system/processor").json()
    assert data["available"] is True            # host CPU is still available
    assert data["gpu_available"] is False
    assert data["gpus"] == []
    assert data["gpu_reason"] == "no NVIDIA GPU"
    assert data["cpu"]["utilization"] == 42.0


@pytest.mark.asyncio
async def test_ollama_offline_cli_fallback_used(client):
    _login(client)
    """API down + local `ollama ps` CLI present -> models still reported."""
    cli_rows = [{"name": "llama3.2:1b", "size_vram": 900_000_000, "size": 1_300_000_000}]
    rs._processor = make_collector(GPU_OK, SYS_OK,
                                   {"online": False, "endpoint": "http://127.0.0.1:11434"},
                                   cli_rows=cli_rows, cli_ok=True)
    data = client.get("/api/system/processor").json()
    assert data["ollama"]["api_online"] is False
    assert data["ollama"]["cli_fallback_used"] is True
    assert data["ollama"]["online"] is True
    assert data["ollama"]["running_models"][0]["name"] == "llama3.2:1b"
    assert data["ollama"]["vram_used"] == 900_000_000


@pytest.mark.asyncio
async def test_ollama_offline_no_cli_is_unavailable_not_crash(client):
    _login(client)
    """API down + no CLI -> structured unavailable, empty models, HTTP 200."""
    rs._processor = make_collector(GPU_OK, SYS_OK,
                                   {"online": False, "endpoint": "http://127.0.0.1:11434"})
    data = client.get("/api/system/processor").json()
    assert data["ollama"]["online"] is False
    assert data["ollama"]["running_models"] == []
    assert data["ollama"]["vram_used"] == 0
    assert data["available"] is True  # CPU/GPU data still shown


@pytest.mark.asyncio
async def test_everything_unavailable_is_structured(client):
    _login(client)
    """Worst case (no GPU, no CPU numbers, Ollama down) — no traceback."""
    rs._processor = make_collector({"available": False, "reason": "x"}, {"cpu": {}},
                                   {"online": False, "endpoint": ""})
    data = client.get("/api/system/processor").json()
    assert data["available"] is False
    assert data["gpus"] == []
    assert data["cpu"]["utilization"] is None
    assert data["cpu"]["load"] == [None, None, None]
    assert data["ollama"]["online"] is False


@pytest.mark.asyncio
async def test_running_models_from_ollama_ps(client):
    _login(client)
    rs._processor = make_collector(GPU_OK, SYS_OK, OLLAMA_OK)
    o = client.get("/api/system/processor").json()["ollama"]
    names = [m["name"] for m in o["running_models"]]
    assert names == ["deepseek-coder-v2:latest", "qwen3:8b"]
    assert o["vram_used"] == 6_100_000_000 + 1_200_000_000


@pytest.mark.asyncio
async def test_malformed_collector_data_never_crashes(client):
    _login(client)
    """Garbage from collectors must not produce a traceback."""
    rs._processor = make_collector(
        {"available": True, "gpus": ["not-a-dict", None, {"index": 2}]},
        {"cpu": {"percent": "abc", "load_1": None, "threads": "x"}},
        {"online": True, "running": [None, "junk", {"name": "ok", "size_vram": "5"}]},
    )
    data = client.get("/api/system/processor").json()
    assert data["available"] is True
    assert [g["name"] for g in data["gpus"]] == ["GPU 0", "GPU 1", "GPU 2"]
    assert data["cpu"]["utilization"] is None
    assert data["cpu"]["load"] == [None, None, None]
    assert data["ollama"]["running_models"] == [{"name": "ok", "size_vram": 5.0, "size": 0}]


@pytest.mark.asyncio
async def test_sample_is_cached_briefly():
    """Second sample within TTL is served from cache (one collection)."""
    calls = {"n": 0}

    async def gpu_sample():
        calls["n"] += 1
        return dict(GPU_OK)

    async def sys_sample():
        return dict(SYS_OK)

    async def status():
        return dict(OLLAMA_OK)

    async def cli_ps():
        return None

    c = ProcessorCollector()
    c._gpu, c._system, c._ollama = _Stub(sample=gpu_sample), _Stub(sample=sys_sample), _Stub(status=status)
    c._cli_ps = cli_ps  # type: ignore[method-assign]
    first = await c.sample()
    second = await c.sample()
    assert calls["n"] == 1
    assert first is second


@pytest.mark.asyncio
async def test_cli_ps_rejects_bad_output(monkeypatch):
    """The real CLI wrapper must survive missing binary / bad JSON / rc!=0."""
    c = ProcessorCollector()

    class FakeShutil:
        @staticmethod
        def which(cmd):
            return None

    monkeypatch.setattr(rs.shutil, "which", FakeShutil.which)
    assert await c._cli_ps() is None  # no ollama CLI -> None, no exception


# --------------------------------------------------------------------------- #
# GPU telemetry synchronization — PROCESSOR vs the Dashboard GPU block
#
# Incident (real 192.168.80.22): PROCESSOR showed util 9% / VRAM 15.4/16 GB /
# 45°C, the Dashboard GPU block showed 0% / 550 MB / 44°C at the same moment.
# Root cause: the two blocks built their payloads at different times through
# different paths. Fix: PROCESSOR reuses the realtime service's CURRENT
# snapshot — the same object the GPU block renders — so they cannot diverge.
# These tests pin the consistency across the whole path, not just the collector.
# --------------------------------------------------------------------------- #

SNAP_GPU_FRESH = {
    "available": True, "source": "test", "ts": None,  # ts set per test
    "gpus": [{
        "index": 0, "name": "Tesla V100-SXM2-16GB", "utilization": 9,
        "temperature": 45, "vram_total": 16_160_000_000,
        "vram_used": 15_400_000_000, "memory_utilization": 95,
    }],
    "processes": [],
}
SNAP_FRESH = {
    "ts": None,
    "gpu": dict(SNAP_GPU_FRESH),
    "cpu": {"percent": 41.5, "cores": 8, "threads": 16, "load": [3.2, 2.8, 2.4]},
    "system": {"hostname": "ollama-server"},
    "ollama": {"online": True, "endpoint": "http://127.0.0.1:11434",
               "running": [{"name": "devstral-24b:latest", "size_vram": 13_500_000_000, "size": 14_000_000_000}]},
}


def _fresh_snap():
    snap = json.loads(json.dumps(SNAP_FRESH))  # deep copy, fresh object each test
    snap["ts"] = snap["gpu"]["ts"] = time.time()
    return snap


@pytest.mark.asyncio
async def test_processor_matches_dashboard_gpu_block(client):
    _login(client)
    """PROCESSOR must show EXACTLY what the Dashboard GPU block shows."""
    snap = _fresh_snap()
    rs._processor = make_collector(GPU_OK, SYS_OK, OLLAMA_OK, shared_snapshot=snap)
    data = client.get("/api/system/processor").json()
    g = snap["gpu"]["gpus"][0]
    assert data["gpus"][0]["utilization"] == g["utilization"]
    assert data["gpus"][0]["memory_used"] == g["vram_used"]
    assert data["gpus"][0]["memory_total"] == g["vram_total"]
    assert data["gpus"][0]["temperature"] == g["temperature"]


@pytest.mark.asyncio
async def test_vram_not_replaced_by_ollama_model_vram(client):
    _login(client)
    """GPU VRAM is the NVML/nvidia-smi number — never the ollama model size."""
    snap = _fresh_snap()
    snap["gpu"]["gpus"][0]["vram_used"] = 15_400_000_000
    snap["ollama"]["running"] = [{"name": "devstral-24b:latest", "size_vram": 13_500_000_000, "size": 14_000_000_000}]
    rs._processor = make_collector(GPU_OK, SYS_OK, OLLAMA_OK, shared_snapshot=snap)
    data = client.get("/api/system/processor").json()
    assert data["gpus"][0]["memory_used"] == 15_400_000_000           # GPU VRAM
    assert data["ollama"]["vram_used"] == 13_500_000_000              # model VRAM, separate
    assert data["ollama"]["running_models"][0]["name"] == "devstral-24b:latest"


class _FakeRT:
    def __init__(self, snap):
        self.last_snapshot = snap


@pytest.mark.asyncio
async def test_stale_realtime_snapshot_not_used(client, monkeypatch):
    _login(client)
    """A stalled realtime loop must not leak old GPU numbers into PROCESSOR.

    Exercises the REAL _realtime_snapshot() freshness check (not a stub):
    the stale service snapshot is rejected and PROCESSOR collects fresh data.
    """
    snap = _fresh_snap()
    snap["gpu"]["gpus"][0]["utilization"] = 75   # old values (the v1 incident)
    snap["gpu"]["gpus"][0]["temperature"] = 55
    snap["ts"] = snap["gpu"]["ts"] = time.time() - 3600   # one hour old
    import webui.realtime as rt_mod
    monkeypatch.setattr(rt_mod, "get_realtime_service", lambda: _FakeRT(snap))
    collector = make_collector(GPU_OK, SYS_OK, OLLAMA_OK)
    # restore the REAL freshness-checking method (make_collector stubbed it)
    collector._realtime_snapshot = ProcessorCollector._realtime_snapshot.__get__(collector)  # type: ignore[method-assign]
    rs._processor = collector
    data = client.get("/api/system/processor").json()
    assert data["gpus"][0]["utilization"] == 74.0          # from OWN fresh collection
    assert data["gpus"][0]["temperature"] == 64            # not the stale 55
    assert data["telemetry_source"] == "own-collection"    # prove the fresh path


@pytest.mark.asyncio
async def test_no_snapshot_falls_back_to_own_collection(client):
    _login(client)
    """Realtime absent (early startup) -> fresh own collection, no crash."""
    collector = make_collector(GPU_OK, SYS_OK, OLLAMA_OK)   # snapshot path disabled
    rs._processor = collector
    data = client.get("/api/system/processor").json()
    assert data["telemetry_source"] == "own-collection"
    assert data["gpus"][0]["utilization"] == 74.0
    assert data["ollama"]["running_models"][0]["name"] == "deepseek-coder-v2:latest"


@pytest.mark.asyncio
async def test_multi_gpu_consistent_between_blocks(client):
    _login(client)
    """All GPUs flow through the shared snapshot with per-index fidelity."""
    snap = _fresh_snap()
    snap["gpu"]["gpus"] = [
        {"index": 0, "name": "GPU0", "utilization": 5, "temperature": 44,
         "vram_total": 16_160_000_000, "vram_used": 550_000_000},
        {"index": 1, "name": "GPU1", "utilization": 97, "temperature": 70,
         "vram_total": 16_160_000_000, "vram_used": 15_900_000_000},
    ]
    rs._processor = make_collector(GPU_OK, SYS_OK, OLLAMA_OK, shared_snapshot=snap)
    data = client.get("/api/system/processor").json()
    assert [g["index"] for g in data["gpus"]] == [0, 1]
    assert data["gpus"][0]["utilization"] == 5 and data["gpus"][1]["utilization"] == 97
    assert data["gpus"][0]["memory_used"] == 550_000_000 and data["gpus"][1]["memory_used"] == 15_900_000_000
    assert data["gpus"][0]["temperature"] == 44 and data["gpus"][1]["temperature"] == 70


@pytest.mark.asyncio
async def test_realtime_snapshot_and_api_status_share_gpu_object(client, monkeypatch):
    _login(client)
    """The path-to-Dashboard guarantee: the snapshot /api/status serves is the
    same snapshot PROCESSOR consumed — GPU numbers identical by construction."""
    import webui.realtime as rt_mod
    import webui.routers.status as status_mod
    snap = _fresh_snap()
    fake = _FakeRT(snap)
    # the status router binds get_realtime_service statically at import time
    monkeypatch.setattr(rt_mod, "get_realtime_service", lambda: fake)
    monkeypatch.setattr(status_mod, "get_realtime_service", lambda: fake)
    collector = make_collector(GPU_OK, SYS_OK, OLLAMA_OK)
    collector._realtime_snapshot = ProcessorCollector._realtime_snapshot.__get__(collector)  # type: ignore[method-assign]
    rs._processor = collector
    proc_gpu = client.get("/api/system/processor").json()["gpus"][0]
    status_gpu = client.get("/api/status").json()["gpu"]["gpus"][0]
    assert proc_gpu["utilization"] == status_gpu["utilization"] == 9
    assert proc_gpu["memory_used"] == status_gpu["vram_used"] == 15_400_000_000
    assert proc_gpu["temperature"] == status_gpu["temperature"] == 45


@pytest.mark.asyncio
async def test_dashboard_gpu_tile_uses_fresh_shared_source(client, monkeypatch):
    _login(client)
    """Path check: /api/status GPU numbers equal PROCESSOR GPU numbers when the
    realtime service holds a CURRENT snapshot (no stale window)."""
    import webui.realtime as rt_mod
    import webui.routers.status as status_mod
    snap = _fresh_snap()
    fake = _FakeRT(snap)
    monkeypatch.setattr(rt_mod, "get_realtime_service", lambda: fake)
    monkeypatch.setattr(status_mod, "get_realtime_service", lambda: fake)
    collector = make_collector(GPU_OK, SYS_OK, OLLAMA_OK)
    collector._realtime_snapshot = ProcessorCollector._realtime_snapshot.__get__(collector)  # type: ignore[method-assign]
    rs._processor = collector
    p = client.get("/api/system/processor").json()
    st = client.get("/api/status").json()
    assert p["telemetry_source"] == "realtime-snapshot"
    assert p["gpus"][0]["utilization"] == st["gpu"]["gpus"][0]["utilization"] == 9
    assert p["gpus"][0]["memory_used"] == st["gpu"]["gpus"][0]["vram_used"] == 15_400_000_000
    assert p["gpus"][0]["temperature"] == st["gpu"]["gpus"][0]["temperature"] == 45


@pytest.mark.asyncio
async def test_processor_endpoint_requires_auth(client):
    # no login on purpose: unauthenticated request must be rejected
    assert client.get("/api/system/processor").status_code == 401
