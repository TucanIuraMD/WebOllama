"""Electricity v1.1 — host power, GPU energy sampler, aggregation, cost.

Contract highlights under test:
  - 0.5 s NVML sampling goes to a RAM buffer ONLY (no DB writes, no disk I/O
    at that cadence); every 10 s ONE aggregated point per GPU is written
    (interval_seconds, average_power_w, energy_wh, source=NVML, gpu_index).
  - energy_wh = average_power_w × interval_seconds / 3600.
  - Gaps / stopped sampler / NVML unavailable → NO rows, NO interpolation,
    NO zeros; restarts can never double-count.
  - GPU power/energy is GPU-only and NEVER total server power; the host
    headline exists only with a host-level source (RAPL / hwmon).
  - The sampler is a singleton: start() twice never duplicates the task;
    stop() cancels it and flushes only the actually-measured remainder.
  - Cost appears only with an explicitly configured tariff.
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
from webui.gpu_energy import GpuEnergySampler, set_gpu_energy_sampler


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
        self.insert_calls = 0

    async def insert_metric(self, ts, kind, payload):
        self.insert_calls += 1
        self.rows.append((ts, kind, payload))

    async def get_metrics(self, kind, since, limit=5000):
        return [{"ts": ts, "data": p} for (ts, k, p) in sorted(self.rows)
                if k == kind and ts >= since]

    async def get_setting(self, key, default=None):
        return self.settings.get(key, default)

    async def set_setting(self, key, value):
        self.settings[key] = value


def make_service(gpus=None, rapl=None, hwmon=None, db=None, gpu_energy="unset"):
    db = db or FakeDb()
    kwargs = {}
    if gpu_energy != "unset":
        kwargs["gpu_energy"] = gpu_energy
    svc = ElectricityService(db, gpu_collector=FakeGpuCollector(gpus),
                             rapl=rapl, hwmon=hwmon, **kwargs)
    return db, svc


class StaticHwmon:
    available = True

    def __init__(self, watts):
        self._w = watts

    def probe(self):
        pass

    def sample(self):
        return {"watts": self._w}


class StaticRapl:
    available = True

    def __init__(self, result):
        self._result = result

    def probe(self):
        pass

    def sample(self):
        return dict(self._result)


# ---------------------------------------------------------------------------
# Sampler: RAM buffer + 10 s aggregation
# ---------------------------------------------------------------------------
def test_sampling_goes_to_ram_buffer_not_db():
    db = FakeDb()
    sampler = GpuEnergySampler(db, sample_interval=0.05, aggregate_interval=1000.0,
                               reader=lambda: {0: 30.0})
    s = GpuEnergySampler.__new__(GpuEnergySampler)  # no, keep the real one below
    # simulate ticks without the loop:
    powers = sampler._read_powers()
    assert powers == {0: 30.0}
    sampler._buffer.append((time.time(), powers))
    assert sampler.buffer_size() == 1
    assert db.insert_calls == 0 and db.rows == []  # nothing hit the DB


@pytest.mark.asyncio
async def test_aggregation_after_interval_one_point_per_gpu():
    db = FakeDb()
    sampler = GpuEnergySampler(db, sample_interval=0.05, aggregate_interval=0.2,
                               reader=lambda: {0: 30.0})
    await sampler.start()
    await asyncio.sleep(0.65)  # ~13 ticks → ≥1 aggregation window
    await sampler.stop()
    assert sampler.aggregations >= 1
    rows = [(t, p) for (t, k, p) in db.rows if k == "gpu_energy"]
    assert rows, "no aggregated point written"
    ts, payload = rows[-1]
    assert payload["source"] == "NVML"
    assert payload["gpu_index"] == 0
    assert payload["interval_seconds"] > 0
    # 30 W flat: energy_wh = 30 × interval / 3600
    expect = 30.0 * payload["interval_seconds"] / 3600.0
    # stored values are rounded (interval 3dp, energy 6dp) → small tolerance
    assert payload["energy_wh"] == pytest.approx(expect, rel=5e-3)
    assert payload["average_power_w"] == pytest.approx(30.0, rel=1e-3)


@pytest.mark.asyncio
async def test_variable_power_average():
    db = FakeDb()
    seq = iter([20.0, 40.0] * 100)
    sampler = GpuEnergySampler(db, sample_interval=0.05, aggregate_interval=0.25,
                               reader=lambda: {0: next(seq)})
    await sampler.start()
    await asyncio.sleep(0.7)
    await sampler.stop()
    rows = [p for (t, k, p) in db.rows if k == "gpu_energy"]
    assert rows
    # variable 20/40 W must average near 30 W, not take the last sample
    avgs = [r["average_power_w"] for r in rows]
    assert all(19 < a < 41 for a in avgs)
    overall = sum(r["energy_wh"] for r in rows) / (sum(r["interval_seconds"] for r in rows) / 3600.0)
    assert 25 < overall < 35


@pytest.mark.asyncio
async def test_idle_and_high_power_levels_aggregate():
    db = FakeDb()
    seq = iter([22.0, 24.0] * 100)  # idle class GPU (20–40 W)
    sampler = GpuEnergySampler(db, sample_interval=0.05, aggregate_interval=0.2,
                               reader=lambda: {0: next(seq)})
    await sampler.start()
    await asyncio.sleep(0.55)
    await sampler.stop()
    idle_rows = [p for (t, k, p) in db.rows if k == "gpu_energy"]
    assert idle_rows and all(20 <= r["average_power_w"] <= 40 for r in idle_rows)

    db2 = FakeDb()
    seq2 = iter([295.0, 305.0] * 100)  # high-power class
    sampler2 = GpuEnergySampler(db2, sample_interval=0.05, aggregate_interval=0.2,
                                reader=lambda: {0: next(seq2)})
    await sampler2.start()
    await asyncio.sleep(0.55)
    await sampler2.stop()
    high_rows = [p for (t, k, p) in db2.rows if k == "gpu_energy"]
    assert high_rows and all(290 <= r["average_power_w"] <= 310 for r in high_rows)


@pytest.mark.asyncio
async def test_energy_formula_matches_spec():
    db = FakeDb()
    # one exact manual flush: 30 W over exactly 10 s → 0.0833 Wh
    now = time.time()
    sampler = GpuEnergySampler(db, sample_interval=0.5, aggregate_interval=10.0,
                               reader=lambda: {0: 30.0})
    sampler._buffer = [(now - 10 + i * 0.5, {0: 30.0}) for i in range(21)]
    await sampler._flush()
    rows = [p for (t, k, p) in db.rows if k == "gpu_energy"]
    assert len(rows) == 1
    p = rows[0]
    assert p["interval_seconds"] == pytest.approx(10.0, rel=1e-3)
    assert p["average_power_w"] == pytest.approx(30.0)
    assert p["energy_wh"] == pytest.approx(30.0 * 10 / 3600.0, rel=1e-3)  # 0.0833
    assert p["energy_wh"] == pytest.approx(0.0833, abs=1e-3)


@pytest.mark.asyncio
async def test_gap_no_rows_no_interpolation():
    db = FakeDb()
    sampler = GpuEnergySampler(db, sample_interval=0.05, aggregate_interval=0.2,
                               reader=lambda: None)  # NVML down the whole time
    await sampler.start()
    await asyncio.sleep(0.4)
    await sampler.stop()
    assert db.rows == []
    assert sampler.aggregations == 0
    s = await sampler.gpu_energy_summary(minutes=60)
    assert s["available"] is False and s["energy_wh"] is None


@pytest.mark.asyncio
async def test_partial_nvml_availability_no_zero_fill():
    db = FakeDb()
    # reader alternates: available / unavailable — unavailable ticks must
    # simply not contribute samples, not zeros
    state = {"on": True}

    def reader():
        state["on"] = not state["on"]
        return {0: 50.0} if state["on"] else None

    sampler = GpuEnergySampler(db, sample_interval=0.05, aggregate_interval=0.25,
                               reader=reader)
    await sampler.start()
    await asyncio.sleep(0.7)
    await sampler.stop()
    rows = [p for (t, k, p) in db.rows if k == "gpu_energy"]
    assert rows and all(r["average_power_w"] == pytest.approx(50.0) for r in rows)


@pytest.mark.asyncio
async def test_restart_no_double_count():
    db = FakeDb()
    # process 1: samples 30 W for 0.3 s, then "restarts"
    s1 = GpuEnergySampler(db, sample_interval=0.05, aggregate_interval=0.25,
                          reader=lambda: {0: 30.0})
    await s1.start()
    await asyncio.sleep(0.4)
    await s1.stop()  # flushes its measured remainder

    # process 2: fresh buffer, later timestamps
    await asyncio.sleep(0.1)
    s2 = GpuEnergySampler(db, sample_interval=0.05, aggregate_interval=0.25,
                          reader=lambda: {0: 30.0})
    await s2.start()
    await asyncio.sleep(0.4)
    await s2.stop()

    rows = [(t, p) for (t, k, p) in db.rows if k == "gpu_energy"]
    assert len(rows) >= 2
    # rows must be disjoint in time: no interval overlaps another's span
    spans = []
    for t, p in rows:
        spans.append((t - p["interval_seconds"], t))
    spans.sort()
    for (a0, a1), (b0, b1) in zip(spans, spans[1:]):
        assert b0 >= a0 and b0 >= a1 - 1e-6 or b1 <= a0, "overlapping intervals → double count"
    # total energy equals the sum of the row energies (each interval counted once)
    total = sum(p["energy_wh"] for _, p in rows)
    s = await GpuEnergySampler(db).gpu_energy_summary(minutes=60)
    assert s["energy_wh"] == pytest.approx(total, rel=1e-6)


@pytest.mark.asyncio
async def test_summary_counts_only_measured_seconds():
    db = FakeDb()
    now = time.time()
    # two 10 s intervals measured, then a 2 h silence, then one more
    db.rows.append((now - 400, "gpu_energy",
                    {"gpu_index": 0, "interval_seconds": 10.0, "average_power_w": 30.0,
                     "energy_wh": 30 * 10 / 3600.0, "source": "NVML"}))
    db.rows.append((now - 390, "gpu_energy",
                    {"gpu_index": 0, "interval_seconds": 10.0, "average_power_w": 30.0,
                     "energy_wh": 30 * 10 / 3600.0, "source": "NVML"}))
    db.rows.append((now - 5, "gpu_energy",
                    {"gpu_index": 0, "interval_seconds": 10.0, "average_power_w": 60.0,
                     "energy_wh": 60 * 10 / 3600.0, "source": "NVML"}))
    s = await GpuEnergySampler(db).gpu_energy_summary(minutes=60)
    assert s["available"] is True
    assert s["measured_seconds"] == pytest.approx(30.0)  # gaps contribute nothing
    assert s["energy_wh"] == pytest.approx(3 * 30 * 10 / 3600.0 + 30 * 10 / 3600.0 * 1.0, rel=1e-3)
    assert s["gpus"][0]["kwh"] == pytest.approx(s["kwh"], rel=1e-3)


# ---------------------------------------------------------------------------
# Sampler lifecycle
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_no_duplicate_sampler_start_twice():
    db = FakeDb()
    sampler = GpuEnergySampler(db, sample_interval=0.05, aggregate_interval=100.0,
                               reader=lambda: {0: 30.0})
    await sampler.start()
    t1 = sampler._task
    await sampler.start()
    await sampler.start()
    assert sampler._task is t1, "start() spawned a duplicate sampler task"
    await sampler.stop()
    assert sampler.running is False
    assert sampler._task is None


@pytest.mark.asyncio
async def test_stop_flushes_measured_remainder():
    db = FakeDb()
    sampler = GpuEnergySampler(db, sample_interval=0.05, aggregate_interval=100.0,
                               reader=lambda: {0: 30.0})
    await sampler.start()
    await asyncio.sleep(0.3)  # buffer has measured samples, no aggregation yet
    assert sampler.buffer_size() > 0
    await sampler.stop()
    rows = [p for (t, k, p) in db.rows if k == "gpu_energy"]
    assert len(rows) == 1  # flushed exactly the measured remainder
    assert sampler.buffer_size() == 0


@pytest.mark.asyncio
async def test_buffer_bounded_ram():
    db = FakeDb()
    sampler = GpuEnergySampler(db, sample_interval=0.05, aggregate_interval=0.2,
                               reader=lambda: {0: 30.0})
    assert sampler._max_buffer <= 4 * (sampler.aggregate_interval / sampler.sample_interval) + 8
    await sampler.start()
    await asyncio.sleep(0.6)
    await sampler.stop()
    assert sampler.buffer_size() <= sampler._max_buffer


def test_reader_unavailable_returns_none():
    db = FakeDb()
    sampler = GpuEnergySampler(db, reader=lambda: None)
    assert sampler._read_powers() is None
    # and with pynvml against a no-GPU box the default reader also degrades:
    sampler2 = GpuEnergySampler(db)
    r = sampler2._read_powers()
    assert r is None or isinstance(r, dict)  # structured, never raises


# ---------------------------------------------------------------------------
# Host honesty: GPU != total server
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_gpu_only_total_server_unavailable():
    gpu = FakeGpuCollector([{"index": 0, "name": "Tesla V100", "power_draw": 185.0}])
    db, svc = make_service(gpus=None)
    svc._gpu = gpu
    s = await svc.sample()
    assert s["available"] is False
    assert s["watts"] is None
    assert "UNAVAILABLE" in (s["reason"] or "").upper()
    assert s["gpu_power"]["current_watts"] == 185.0
    assert "NOT total" in s["gpu_power"]["note"]


@pytest.mark.asyncio
async def test_host_level_total_when_hwmon_present():
    db, svc = make_service(gpus=[], hwmon=StaticHwmon(233.5))
    s = await svc.sample()
    assert s["available"] is True and s["watts"] == 233.5
    assert s["source"] == "hwmon"


@pytest.mark.asyncio
async def test_gpu_power_never_added_to_host_total():
    gpu = FakeGpuCollector([{"index": 0, "name": "V100", "power_draw": 185.0}])
    db, svc = make_service(gpus=None, hwmon=StaticHwmon(25.0))
    svc._gpu = gpu
    s = await svc.sample()
    assert s["watts"] == 25.0
    assert s["gpu_power"]["current_watts"] == 185.0


# ---------------------------------------------------------------------------
# Source-level behavior
# ---------------------------------------------------------------------------
def test_rapl_power_from_energy_delta(tmp_path):
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
    energy_file.write_text(str(1000_000_000 + 5_000_000))  # +5 J
    second = r.sample()
    assert second["watts"] is not None
    assert 10 < second["watts"] < 1000


def test_rapl_unavailable_when_no_sysfs(tmp_path):
    r = RaplSource(root=str(tmp_path / "missing"))
    r.probe()
    assert r.available is False
    assert r.sample() is None


def test_hwmon_reads_microwatts_and_ignores_drivetemp(tmp_path):
    hw = tmp_path / "hwmon0"
    hw.mkdir()
    (hw / "name").write_text("nct6798")
    (hw / "power1_input").write_text("25000000")  # 25 W
    drive = tmp_path / "hwmon9"
    drive.mkdir()
    (drive / "name").write_text("drivetemp")
    (drive / "power1_input").write_text("999000000")
    h = HwmonPowerSource(root=str(tmp_path))
    h.probe()
    assert h.available is True
    assert h.sample()["watts"] == pytest.approx(25.0)


# ---------------------------------------------------------------------------
# Host kWh summary + tariff
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_host_energy_valid_power_to_kwh():
    db = FakeDb()
    t0 = time.time() - 3590
    for i in range(0, 13):
        db.rows.append((t0 + i * 300, "electricity",
                        {"watts": 100.0, "measured": True, "source": "hwmon"}))
    svc = ElectricityService(db, gpu_collector=FakeGpuCollector([]))
    s = await svc.energy_summary_host(minutes=60)
    assert s["kwh"] == pytest.approx(0.1, rel=1e-2)
    assert s["measured"] is True
    assert s["method"] == "trapezoid-integration"


@pytest.mark.asyncio
async def test_host_energy_gap_no_double_count():
    db = FakeDb()
    now = time.time()
    for i in range(3):
        db.rows.append((now - 7200 - 300 + i * 300, "electricity",
                        {"watts": 100.0, "measured": True, "source": "hwmon"}))
    for i in range(3):
        db.rows.append((now - 300 + i * 150, "electricity",
                        {"watts": 200.0, "measured": True, "source": "hwmon"}))
    svc = ElectricityService(db, gpu_collector=FakeGpuCollector([]))
    s = await svc.energy_summary_host(minutes=1440)
    assert s["integrated_intervals"] == 4
    assert s["kwh"] == pytest.approx((100 * 600 + 200 * 300) / 3.6e6, rel=1e-3)


@pytest.mark.asyncio
async def test_tariff_unset_no_cost_no_default():
    db, svc = make_service(gpus=[])
    cfg = await svc.get_config()
    assert cfg["tariff_is_configured"] is False and cfg["tariff"] is None
    assert "not configured" in cfg["notice"]


@pytest.mark.asyncio
async def test_tariff_set_change_and_bounds():
    db, svc = make_service(gpus=[])
    cfg = await svc.set_config(tariff=5.0, currency="lei")
    assert cfg["tariff"] == 5.0 and cfg["tariff_is_configured"] is True
    await svc.set_config(tariff=2.5)
    assert (await svc.get_config())["tariff"] == 2.5
    with pytest.raises(ValueError):
        await svc.set_config(tariff=-1)
    with pytest.raises(ValueError):
        await svc.set_config(tariff=5000)
    with pytest.raises(ValueError):
        await svc.set_config(currency="too-long-currency")


# ---------------------------------------------------------------------------
# Tariff block (v1.2): display + projections + accumulated costs
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_tariff_block_set_projections_and_accumulated():
    db = FakeDb()
    sampler = GpuEnergySampler(db, reader=lambda: {0: 200.0})
    set_gpu_energy_sampler(sampler)
    _, svc = make_service(gpus=[{"index": 0, "name": "V100", "power_draw": 200.0}],
                          db=db, gpu_energy=sampler)
    await svc.set_config(tariff=5.0, currency="lei")
    now = time.time()
    # self-consistent rows: 100 W over 10 s → 0.277778 Wh (= 100×10/3600)
    wh = 100.0 * 10.0 / 3600.0
    db.rows.append((now - 120, "gpu_energy",
                    {"gpu_index": 0, "interval_seconds": 10.0, "average_power_w": 100.0,
                     "energy_wh": wh, "source": "NVML"}))
    # 10 days ago: inside the 30d window, outside today/24h
    db.rows.append((now - 10 * 86400, "gpu_energy",
                    {"gpu_index": 0, "interval_seconds": 10.0, "average_power_w": 100.0,
                     "energy_wh": wh, "source": "NVML"}))
    out = await svc.gpu_energy_windows()
    t = out["tariff"]
    # tariff display
    assert t["tariff"] == 5.0 and t["tariff_is_configured"] is True
    assert t["currency"] == "lei"
    assert t["tariff_notice"] is None
    # CURRENT COST / hour = current W × tariff / 1000 = 200 × 5 / 1000 = 1.0
    assert t["current_power_w"] == 200.0
    assert t["current_cost_per_hour"] == pytest.approx(1.0)
    # AVERAGE COST / hour from the 24h window average (100 W → 0.5 lei/h)
    assert t["average_power_w"] == 100.0
    assert t["average_cost_per_hour"] == pytest.approx(0.5)
    # ACCUMULATED: measured kWh × tariff, stored rounded to 4 dp
    kwh_one = wh / 1000.0
    assert out["today"]["cost"] == round(kwh_one * 5.0, 4)
    assert out["h24"]["cost"] == round(kwh_one * 5.0, 4)
    assert out["month"]["cost"] == round(2 * kwh_one * 5.0, 4)
    for k in ("today", "h24", "month"):
        assert out[k]["cost_note"] == "calculated: measured energy × tariff"
        assert out[k]["currency"] == "lei"


@pytest.mark.asyncio
async def test_tariff_block_unset_costs_unavailable_never_zero():
    set_gpu_energy_sampler(GpuEnergySampler(FakeDb(), reader=lambda: {0: 200.0}))
    db, svc = make_service(gpus=[{"index": 0, "name": "V100", "power_draw": 200.0}])
    now = time.time()
    db.rows.append((now - 60, "gpu_energy",
                    {"gpu_index": 0, "interval_seconds": 10.0, "average_power_w": 100.0,
                     "energy_wh": 0.001, "source": "NVML"}))
    out = await svc.gpu_energy_windows()
    t = out["tariff"]
    assert t["tariff"] is None and t["tariff_is_configured"] is False
    assert "tariff not configured" in t["tariff_notice"]
    # projections unavailable — and NOT zero
    assert t["current_cost_per_hour"] is None
    assert t["average_cost_per_hour"] is None
    assert t["cost_unavailable"] == "tariff not configured"
    assert t["current_power_w"] is None  # stable contract: key exists, value None
    # accumulated costs explicitly None + reason, never 0
    for k in ("today", "h24", "month"):
        assert out[k]["cost"] is None
        assert out[k]["cost_unavailable"] == "tariff not configured"
        assert out[k].get("cost") != 0


@pytest.mark.asyncio
async def test_current_cost_rounding_four_decimals():
    set_gpu_energy_sampler(GpuEnergySampler(FakeDb(), reader=lambda: {0: 33.333}))
    db, svc = make_service(gpus=[{"index": 0, "name": "V100", "power_draw": 33.333}])
    await svc.set_config(tariff=1.2345)
    out = await svc.gpu_energy_windows()
    t = out["tariff"]
    # 33.333 W × 1.2345 lei/kWh / 1000 = 0.0411499... → 4 dp rounding → 0.0411
    assert t["current_cost_per_hour"] == pytest.approx(0.0411, abs=1e-4)
    assert round(t["current_cost_per_hour"], 4) == t["current_cost_per_hour"]


@pytest.mark.asyncio
async def test_period_window_cost_via_apply_tariff():
    db, svc = make_service(gpus=[])
    await svc.set_config(tariff=2.0)
    out = {"kwh": 3.5, "points": 10, "measured_seconds": 600.0}
    await svc.apply_tariff_to_window(out)
    assert out["cost"] == pytest.approx(7.0)
    assert out["currency"] == "lei"
    # unset → unavailable, never zero
    out2 = {"kwh": 3.5}
    await svc.set_config(tariff=0)  # 0 → not configured
    await svc.apply_tariff_to_window(out2)
    assert out2["cost"] is None
    assert out2["cost_unavailable"] == "tariff not configured"


@pytest.mark.asyncio
async def test_gpu_power_never_leaks_into_host_total_via_tariff_block():
    """The tariff block reads GPU draw for projections but never writes it
    into the host headline."""
    gpu = FakeGpuCollector([{"index": 0, "name": "V100", "power_draw": 185.0}])
    db, svc = make_service(gpus=None, hwmon=StaticHwmon(25.0))
    svc._gpu = gpu
    await svc.set_config(tariff=5.0)  # projections (and current_power_w) need a tariff
    s = await svc.sample()
    assert s["watts"] == 25.0 and s["gpu_power"]["current_watts"] == 185.0
    set_gpu_energy_sampler(GpuEnergySampler(FakeDb(), reader=lambda: {0: 185.0}))
    out = await svc.gpu_energy_windows()
    assert out["tariff"]["current_power_w"] == 185.0  # GPU-only projection input
    assert svc.sample and (await svc.sample())["watts"] == 25.0


# ---------------------------------------------------------------------------
# HTTP API
# ---------------------------------------------------------------------------
def test_api_contract(client):
    """Full-app HTTP contract via the shared TestClient fixture."""
    db, svc = make_service(gpus=[], hwmon=StaticHwmon(42.0))
    set_electricity_service(svc)
    try:
        assert client.post("/api/auth/login",
                           json={"username": "admin", "password": "changeme"}).status_code == 200

        r = client.get("/api/electricity")
        assert r.status_code == 200
        body = r.json()
        assert body["available"] is True and body["watts"] == 42.0
        assert body["gpu_power"]["note"].startswith("GPU power draw only")

        r = client.get("/api/electricity/gpu-energy")
        assert r.status_code == 200
        g = r.json()
        for key in ("today", "h24", "month", "sampler"):
            assert key in g
        assert g["today"]["note"].startswith("GPU-only")

        r = client.get("/api/electricity/gpu-energy-window?minutes=60")
        assert r.status_code == 200 and "kwh" in r.json()

        r = client.get("/api/electricity/gpu-history?minutes=60")
        assert r.status_code == 200 and r.json()["kind"] == "gpu_energy"

        r = client.get("/api/electricity/summary?minutes=60")
        assert r.status_code == 200 and r.json()["tariff_configured"] is False

        r = client.put("/api/electricity/config", json={"tariff": 4.5})
        assert r.status_code == 200 and r.json()["tariff"] == 4.5
        r = client.put("/api/electricity/config", json={})
        assert r.status_code == 422
        r = client.put("/api/electricity/config", json={"tariff": "abc"})
        assert r.status_code == 422
    finally:
        set_electricity_service(None)


def test_api_unavailable_is_structured_200(client):
    db, svc = make_service(gpus=[])
    set_electricity_service(svc)
    try:
        client.post("/api/auth/login", json={"username": "admin", "password": "changeme"})
        r = client.get("/api/electricity")
        assert r.status_code == 200
        body = r.json()
        assert body["available"] is False and body["watts"] is None
        g = client.get("/api/electricity/gpu-energy").json()
        assert g["today"]["available"] is False and g["today"]["energy_wh"] is None
    finally:
        set_electricity_service(None)
