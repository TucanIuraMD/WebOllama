"""Electricity v1 — power sources, energy integration, cost, API.

Contract highlights under test:
  - RAPL/hwmon are the only HOST-level sources; GPU power_draw is a
    reference only and never becomes the headline total.
  - Unavailable sources are structured, never tracebacks; no invented
    totals, no invented tariff defaults.
  - kWh = trapezoidal integration of measured watts over time, gap-safe
    (restarts / retention pruning never double-count).
  - Cost appears only with an explicitly configured tariff.
  - Persistence reuses metrics(kind="electricity") on the 5s metrics loop;
    a sample() call never writes to the DB and the loop never polls a
    source twice outside its TTL.
"""
import asyncio
import time

import pytest

from webui.electricity import (
    ElectricityService,
    HwmonPowerSource,
    RaplSource,
    set_electricity_service,
)


# ---------------------------------------------------------------------------
# Fakes / helpers
# ---------------------------------------------------------------------------
class FakeGpuCollector:
    """Stands in for GPUCollector; returns a canned NVML-style payload."""

    def __init__(self, gpus=None, available=True):
        self.gpus = gpus if gpus is not None else []
        self.available = available
        self.sample_calls = 0

    async def sample(self):
        self.sample_calls += 1
        return {"available": self.available, "gpus": self.gpus, "ts": time.time()}


class FakeDb:
    """In-memory stand-in for the Database metrics/settings surface."""

    def __init__(self):
        self.rows = []  # (ts, kind, payload)
        self.settings = {}

    async def insert_metric(self, ts, kind, payload):
        self.rows.append((ts, kind, payload))

    async def get_metrics(self, kind, since, limit=5000):
        return [{"ts": ts, "data": p} for (ts, k, p) in sorted(self.rows)
                if k == kind and ts >= since]

    async def get_setting(self, key, default=None):
        return self.settings.get(key, default)

    async def set_setting(self, key, value):
        self.settings[key] = value


def make_service(gpus=None, rapl=None, hwmon=None):
    db = FakeDb()
    svc = ElectricityService(db, gpu_collector=FakeGpuCollector(gpus),
                             rapl=rapl, hwmon=hwmon)
    return db, svc


# ---------------------------------------------------------------------------
# Source-level behavior
# ---------------------------------------------------------------------------
def test_rapl_unavailable_when_no_sysfs(tmp_path):
    r = RaplSource(root=str(tmp_path / "missing"))
    r.probe()
    assert r.available is False
    assert r.sample() is None


def test_rapl_power_from_energy_delta(tmp_path):
    import os
    dom = tmp_path / "intel-rapl:0"
    dom.mkdir()
    (dom / "name").write_text("core-rapl")
    energy_file = dom / "energy_uj"
    energy_file.write_text("1000000000")  # 1000 J

    r = RaplSource(root=str(tmp_path))
    r.probe()
    first = r.sample()
    assert first is not None and first["watts"] is None  # 1st sample: no delta yet

    time.sleep(0.05)
    energy_file.write_text("1000000000" if False else str(1000_000_000 + 5_000_000))  # +5 J
    second = r.sample()
    assert second["watts"] is not None
    # 5 J over ~0.05s → roughly 100 W; assert a sane band, not a race-y exact value
    assert 10 < second["watts"] < 1000


def test_rapl_skips_unreadable_domain(tmp_path):
    dom = tmp_path / "intel-rapl:0"
    dom.mkdir()
    (dom / "name").write_text("core-rapl")
    (dom / "energy_uj").write_text("1000")
    dead = tmp_path / "intel-rapl:1"
    dead.mkdir()
    (dead / "name").write_text("psys")
    # no energy_uj file at all — probe still registers the domain, sample must skip it

    r = RaplSource(root=str(tmp_path))
    r.probe()
    s = r.sample()
    assert s is not None and s["joules"] == 0.001


def test_hwmon_unavailable_and_nvme_not_system_power(tmp_path):
    h = HwmonPowerSource(root=str(tmp_path / "missing"))
    h.probe()
    assert h.available is False

    # a drive-level sensor chip must NOT count as system power
    drive = tmp_path / "hwmon9"
    drive.mkdir()
    (drive / "name").write_text("drivetemp")
    (drive / "power1_input").write_text("999000000")
    h2 = HwmonPowerSource(root=str(tmp_path))
    h2.probe()
    assert h2.available is False


def test_hwmon_power_reads_microwatts(tmp_path):
    hw = tmp_path / "hwmon0"
    hw.mkdir()
    (hw / "name").write_text("nct6798")
    (hw / "power1_input").write_text("25000000")  # 25 W
    (hw / "power2_input").write_text("1000000")   # 1 W
    h = HwmonPowerSource(root=str(tmp_path))
    h.probe()
    assert h.available is True
    assert h.sample()["watts"] == pytest.approx(26.0)


# ---------------------------------------------------------------------------
# Service: total power honesty
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_no_sources_means_unavailable_never_fake_total():
    db, svc = make_service(gpus=[])
    s = await svc.sample()
    assert s["available"] is False
    assert s["watts"] is None
    assert s["measured"] is False
    assert "UNAVAILABLE" in (s["reason"] or "").upper()


@pytest.mark.asyncio
async def test_gpu_only_is_reference_not_total():
    # Host has GPU power (NVML) but NO host-level source: headline must stay
    # unavailable; GPU watts ride along as an explicitly-labeled reference.
    gpu = FakeGpuCollector([{"index": 0, "name": "Tesla V100", "power_draw": 185.0}])
    db, svc = make_service(gpus=gpu.gpus)
    svc._gpu = gpu
    s = await svc.sample()
    assert s["available"] is False
    assert s["watts"] is None
    assert s["gpu_power"]["watts"] == 185.0
    assert s["gpu_power"]["measured"] is True
    assert "NOT total" in s["gpu_power"]["note"]


@pytest.mark.asyncio
async def test_hwmon_only_is_a_valid_total():
    hw = tmp_hwmon(30.0)
    db, svc = make_service(gpus=[], hwmon=hw)
    s = await svc.sample()
    assert s["available"] is True
    assert s["measured"] is True
    assert s["watts"] == 30.0
    assert s["source"] == "hwmon"


@pytest.mark.asyncio
async def test_rapl_plus_hwmon_sum_is_total():
    hw = tmp_hwmon(10.0)
    rapl = StaticRapl({"watts": 40.0})
    db, svc = make_service(gpus=[], rapl=rapl, hwmon=hw)
    s = await svc.sample()
    assert s["available"] is True
    assert s["watts"] == 50.0
    assert "rapl" in s["source"] and "hwmon" in s["source"]


@pytest.mark.asyncio
async def test_gpu_power_never_adds_to_host_total():
    gpu = FakeGpuCollector([{"index": 0, "name": "V100", "power_draw": 185.0}])
    hw = tmp_hwmon(25.0)
    db, svc = make_service(gpus=None, hwmon=hw)
    svc._gpu = gpu
    s = await svc.sample()
    assert s["available"] is True
    assert s["watts"] == 25.0  # GPU's 185 W must NOT be summed into the total
    assert s["gpu_power"]["watts"] == 185.0


@pytest.mark.asyncio
async def test_rapl_first_sample_reports_no_watts_yet():
    rapl = StaticRapl({"watts": None})  # probed, but ΔJ/Δt not ready
    db, svc = make_service(gpus=[], rapl=rapl)
    s = await svc.sample()
    assert s["available"] is False
    assert "first sample" in (s["components"]["cpu"]["source"] or "")


@pytest.mark.asyncio
async def test_gpu_collector_crash_never_breaks_sample():
    class ExplodingGpu:
        async def sample(self):
            raise RuntimeError("NVML exploded")

    hw = tmp_hwmon(20.0)
    db, svc = make_service(hwmon=hw)
    svc._gpu = ExplodingGpu()
    s = await svc.sample()
    assert s["available"] is True and s["watts"] == 20.0


# ---------------------------------------------------------------------------
# Persistence + energy integration
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_record_persists_only_when_available():
    db, svc = make_service(gpus=[])  # nothing available
    await svc.record()
    assert db.rows == []

    svc2_hw = tmp_hwmon(50.0)
    db2, svc2 = make_service(hwmon=svc2_hw)
    await svc2.record()
    assert len(db2.rows) == 1
    ts, kind, payload = db2.rows[0]
    assert kind == "electricity"
    assert payload["watts"] == 50.0
    assert payload["measured"] is True


@pytest.mark.asyncio
async def test_energy_integration_valid_power_to_kwh():
    db = FakeDb()
    # 100 W flat for exactly 1 hour → 0.1 kWh (start slightly inside the window)
    t0 = time.time() - 3590
    for i in range(0, 13):
        db.rows.append((t0 + i * 300, "electricity",
                        {"watts": 100.0, "measured": True, "source": "hwmon"}))
    svc = ElectricityService(db, gpu_collector=FakeGpuCollector([]))
    s = await svc.energy_summary(minutes=60)
    assert s["kwh"] == pytest.approx(0.1, rel=1e-3)
    assert s["measured"] is True
    assert s["method"] == "trapezoid-integration"


@pytest.mark.asyncio
async def test_energy_empty_window_is_none_not_zero():
    db, svc = make_service(gpus=[])
    s = await svc.energy_summary(minutes=60)
    assert s["kwh"] is None
    assert s["points"] == 0
    assert s["measured"] is False


@pytest.mark.asyncio
async def test_energy_skips_restart_gap_no_double_count():
    db = FakeDb()
    now = time.time()
    # 100 W for 10 min before an app restart, silence for 2 h, 200 W after
    for i in range(3):
        db.rows.append((now - 7200 - 300 + i * 300, "electricity",
                        {"watts": 100.0, "measured": True, "source": "hwmon"}))
    for i in range(3):
        db.rows.append((now - 300 + i * 150, "electricity",
                        {"watts": 200.0, "measured": True, "source": "hwmon"}))
    svc = ElectricityService(db, gpu_collector=FakeGpuCollector([]))
    s = await svc.energy_summary(minutes=1440)
    # gap > 600s must not be integrated across:
    assert s["integrated_intervals"] == 2 + 2  # 2 within each side
    # sanity: pre-restart side integrates 100W×600s = 16.67 Wh; post side 200W×300s = 16.67 Wh
    assert s["kwh"] == pytest.approx((100 * 600 + 200 * 300) / 3.6e6, rel=1e-3)


@pytest.mark.asyncio
async def test_energy_marks_unmeasured_points():
    db = FakeDb()
    now = time.time()
    db.rows.append((now - 600, "electricity",
                    {"watts": 100.0, "measured": True, "source": "hwmon"}))
    db.rows.append((now, "electricity",
                    {"watts": 100.0, "measured": False, "source": "nvme-estimate"}))
    svc = ElectricityService(db, gpu_collector=FakeGpuCollector([]))
    s = await svc.energy_summary(minutes=60)
    assert s["kwh"] is not None
    assert s["measured"] is False
    assert s["energy_basis"] == "calculated-from-partially-unavailable-power"


# ---------------------------------------------------------------------------
# Tariff / cost
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_tariff_unset_no_invented_default_no_cost():
    db, svc = make_service(gpus=[])
    cfg = await svc.get_config()
    assert cfg["tariff_is_configured"] is False
    assert cfg["tariff"] is None
    assert cfg["notice"] and "not configured" in cfg["notice"]


@pytest.mark.asyncio
async def test_tariff_set_and_cost_calculation():
    db, svc = make_service(gpus=[])
    cfg = await svc.set_config(tariff=5.0, currency="lei")
    assert cfg["tariff_is_configured"] is True
    assert cfg["tariff"] == 5.0

    now = time.time()
    # 1000 W flat for ~2 h, sampled every 10 min (intervals under the 600s gap limit)
    for i in range(13):
        db.rows.append((now - 7190 + i * 600, "electricity",
                        {"watts": 1000.0, "measured": True, "source": "hwmon"}))
    s = await svc.energy_summary(minutes=120)
    assert s["kwh"] == pytest.approx(2.0, rel=1e-2)
    # router-level cost (service returns kWh only; cost = kWh × tariff)
    assert s["kwh"] * 5.0 == pytest.approx(10.0, rel=1e-2)


@pytest.mark.asyncio
async def test_tariff_change_reflects_immediately():
    db, svc = make_service(gpus=[])
    await svc.set_config(tariff=1.0)
    cfg1 = await svc.get_config()
    await svc.set_config(tariff=2.5)
    cfg2 = await svc.get_config()
    assert cfg1["tariff"] == 1.0 and cfg2["tariff"] == 2.5


@pytest.mark.asyncio
async def test_tariff_validation_bounds():
    db, svc = make_service(gpus=[])
    with pytest.raises(ValueError):
        await svc.set_config(tariff=-1)
    with pytest.raises(ValueError):
        await svc.set_config(tariff=5000)
    with pytest.raises(ValueError):
        await svc.set_config(currency="too-long-currency")


# ---------------------------------------------------------------------------
# Probe caching — no duplicate polling
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_probe_cache_no_duplicate_sysfs_polling():
    class CountingHwmon(HwmonPowerSource):
        def __init__(self, root):
            super().__init__(root)
            self.probe_calls = 0

        def probe(self):
            self.probe_calls += 1
            return super().probe()

    hw = CountingHwmon("/nonexistent")
    db, svc = make_service(gpus=[], hwmon=hw)
    for _ in range(5):
        await svc.sample()
    # PROBE_INTERVAL throttling: repeated samples within the window must not re-probe
    assert hw.probe_calls == 1

    # force the window to expire → exactly one more probe
    svc._last_probe -= 61
    await svc.sample()
    assert hw.probe_calls == 2


# ---------------------------------------------------------------------------
# HTTP API
# ---------------------------------------------------------------------------
def test_api_endpoints_contract(client):
    """Full-app HTTP contract via the shared TestClient fixture."""
    db, svc = make_service(gpus=[], hwmon=tmp_hwmon(42.0))
    set_electricity_service(svc)
    try:
        r = client.post("/api/auth/login",
                        json={"username": "admin", "password": "changeme"})
        assert r.status_code == 200

        r = client.get("/api/electricity")
        assert r.status_code == 200
        body = r.json()
        assert body["available"] is True
        assert body["watts"] == 42.0
        assert body["gpu_power"]["note"].startswith("GPU power draw only")

        r = client.get("/api/electricity/history?minutes=60")
        assert r.status_code == 200
        assert r.json()["kind"] == "electricity"

        r = client.get("/api/electricity/summary?minutes=60")
        assert r.status_code == 200
        assert r.json()["tariff_configured"] is False

        # tariff write requires admin (we are admin here)
        r = client.put("/api/electricity/config", json={"tariff": 4.5})
        assert r.status_code == 200
        assert r.json()["tariff"] == 4.5
        r = client.get("/api/electricity/summary?minutes=60")
        assert r.json()["tariff_configured"] is True

        r = client.put("/api/electricity/config", json={})
        assert r.status_code == 422
        r = client.put("/api/electricity/config", json={"tariff": "abc"})
        assert r.status_code == 422
    finally:
        set_electricity_service(None)


def test_api_unavailable_is_structured_200(client):
    """No host source → 200 + honest structured payload, never a 500."""
    db, svc = make_service(gpus=[])
    set_electricity_service(svc)
    try:
        client.post("/api/auth/login", json={"username": "admin", "password": "changeme"})
        r = client.get("/api/electricity")
        assert r.status_code == 200
        body = r.json()
        assert body["available"] is False
        assert body["watts"] is None
        assert "reason" in body
    finally:
        set_electricity_service(None)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def tmp_hwmon(watts: float):
    """A static in-memory hwmon source returning a fixed wattage."""
    class StaticHwmon:
        available = True

        def probe(self):
            pass

        def sample(self):
            return {"watts": watts}

    return StaticHwmon()


class StaticRapl:
    """A RAPL source whose sample() returns a canned dict."""

    def __init__(self, result):
        self._result = result
        self.available = True

    def probe(self):
        pass

    def sample(self):
        return dict(self._result)
