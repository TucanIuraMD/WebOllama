# Electricity v1.2 — host power + GPU energy & cost tracking

**Status:** implemented (v1.2) · **Scope:** the LOCAL machine running WebOllama only.
No SSH, no remote agents, no sudo, no per-request subprocesses. Drive-level
sensors and drive temperature/telemetry of any kind are out of scope:
WebOllama monitors only host-level power sources and GPU telemetry.

Electricity is a separate page (`#electricity`, ⚡ in the sidebar, linked as a
compact card from the Dashboard) with two strictly separated blocks:

1. **Total server power** — host-level telemetry only (RAPL / hwmon). When
   neither is available the headline is **“TOTAL SERVER — data unavailable”**
   with the reason. GPU numbers are never substituted for it.
2. **GPU energy** — GPU-only telemetry via NVML, always labeled
   *“GPU-only — NOT total server energy”*: current W, average W, energy
   today / 24 h / selected period / 30 d, per-GPU split, and cost when a
   tariff is configured.

---

## 1. Measured vs calculated (the core contract)

| Value | Kind | Source of truth |
|---|---|---|
| **Total server power (W)** | **MEASURED** — only with a host-level source | RAPL `energy_uj` (ΔJ/Δt) or hwmon `power*_input` |
| **GPU current power (W)** | MEASURED | NVML `power_draw` (realtime snapshot) |
| **GPU average power (W)** | CALCULATED | interval-weighted mean of aggregated points |
| **GPU energy (Wh / kWh)** | **CALCULATED** | `average_power_w × interval_seconds / 3600` over really measured intervals |
| **CURRENT COST / AVERAGE COST (per hour)** | **CALCULATED — projection** | `power_w × tariff / 1000` — “if this power is sustained for one hour” |
| **COST TODAY / 24H / 30D / period** | **CALCULATED — accumulated** | `measured energy (kWh) × tariff` — for energy already measured |

Hard rules enforced in code, API and UI:

1. **GPU power/energy is never total server power.** Different API fields,
   different UI blocks, explicit labels everywhere.
2. **No invented tariff.** Until the admin saves one, cost is never computed
   and a *“tariff not configured”* notice is shown. No default lei/kWh.
3. **No fabricated readings.** Missing sources → `—` / `unavailable` + reason;
   gaps are never zero-filled, never interpolated.

## 1.1 Tariff and cost (v1.2)

The GPU energy card shows cost in three clearly separated layers:

| Layer | UI label | Formula | Meaning |
|---|---|---|---|
| Tariff banner | **TARIFF:** `X.XX lei / kWh` | — | the currently configured tariff (or *tariff not configured*) |
| Projection | **CURRENT COST** `X.XXX lei / hour` | `current_power_w × tariff / 1000` | forecast if the current GPU power is sustained for 1 h |
| Projection | **AVERAGE COST** `X.XXX lei / hour` | `average_power_w (24 h) × tariff / 1000` | forecast if the 24 h average GPU power is sustained for 1 h |
| Accumulated | **COST TODAY / 24H / 30D / selected period** `X.XX lei` | `kwh × tariff` | the actual cost of already-measured GPU energy |

* Projections and accumulated amounts are labeled as such in the UI
  (*“projection: … sustained 1 h”* vs *“accumulated: measured energy × tariff”*)
  and in the API (`current_cost_per_hour` / `average_cost_per_hour` vs
  per-window `cost` with `cost_note`).
* **No tariff → nothing is zero.** Every cost field is `null` +
  `cost_unavailable: "tariff not configured"`; the UI shows
  *“unavailable — tariff not configured”* instead of `0 lei`.
* The tariff is changed in **Tariff settings** on the same page (admin,
  `PUT /api/electricity/config`); costs re-render immediately after save.
* Hourly costs render at 3 decimals, accumulated amounts at 2; the API stores
  4-decimal values so tests can pin exact rounding.
* All of this is GPU-only — the host headline (TOTAL SERVER) is unaffected.

## 2. GPU energy sampling — the 0.5 s / 10 s pipeline

Implemented in `webui/gpu_energy.py` (`GpuEnergySampler`), started and
stopped together with the realtime service:

```
every 0.5 s   NVML nvmlDeviceGetPowerUsage per GPU  (C call — no subprocess,
              no disk I/O)  →  append (ts, {gpu_index: watts}) to a bounded
              RAM buffer. NOTHING is written to the DB at this cadence.

every 10 s    aggregate the buffer:
                average_power_w  = mean of that GPU's samples in the window
                interval_seconds = t_last − t_first of the window
                energy_wh        = average_power_w × interval_seconds / 3600
              → ONE row per GPU into metrics(kind="gpu_energy")
              → buffer cleared.

payload       {gpu_index, interval_seconds, average_power_w, energy_wh,
               source: "NVML"}   (timestamp is the row's `ts` column)
```

Example: 30 W × 10 s / 3600 = **0.0833 Wh** per 10 s point.

Buffer guarantees:

* **RAM only** — bounded to 4 aggregation windows (overflow drops the oldest
  sample); cleared after every aggregation; no disk I/O at 0.5 s cadence.
* **No duplicate sampler** — `start()` is idempotent (the second call sees a
  live task and returns); the singleton lives in `webui.gpu_energy._sampler`.
* **Shutdown** — `stop()` cancels the loop task and flushes the remainder:
  the last *actually measured* partial interval is aggregated and written,
  so shutdown never silently drops measured energy. (A remainder shorter
  than one sample simply leaves no row.)
* **NVML read pattern** — `nvmlInit()/nvmlShutdown()` per read, mirroring the
  existing `GPUCollector`; a failing GPU or a failed init returns `None` /
  skips that sample, never crashes the loop.

## 3. Gaps and restarts (no silent interpolation, no double counting)

* NVML unavailable or sampler stopped → **no rows** for that period. Energy
  summaries integrate *only* persisted aggregated rows, so unmeasured time
  contributes nothing.
* Each row carries its own `interval_seconds`; a single process writes rows
  with disjoint time spans, so within a run nothing can be counted twice.
* A restart starts with an empty buffer and later timestamps; rows on the
  two sides of the restart cannot overlap, so a restart can never double-count.
* The UI shows `measured_seconds` next to every window so gaps are visible
  instead of being silently bridged.

## 4. Host-level sources (total server power)

| Source | Path | Units | Notes |
|---|---|---|---|
| RAPL | `/sys/class/powercap/intel-rapl:*/energy_uj` | µJ counter → W via ΔJ/Δt | Root-only (0400) in many containers — denial is a structured, first-class outcome. Wrap/reset guard: Δ outside `[0, 1e6 J)` or Δt outside `(0, 60 s)` is discarded. First sample after probe reports no watts (Δ needs two points). |
| hwmon | `/sys/class/hwmon/hwmon*/power*_input` | µW | Chips named `drivetemp` are excluded — drive sensors are not system power. |

Source discovery is cached for **60 s** (`PROBE_INTERVAL`); repeated samples
never re-scan sysfs.

> **Verified on the current deployment host:** RAPL `energy_uj` is mode 0400
> `nobody:nogroup` and returns EACCES **even for uid 0** (container without
> perf-events/MSR privilege); no hwmon chip exposes `power*_input`;
> `/sys/class/power_supply` is empty; no IPMI device/ipmitool exists; no NVML
> GPU on the dev instance. Hence the page honestly reports
> **TOTAL SERVER — data unavailable** here; a host with working RAPL/hwmon
> lights the total up with no code changes.

## 5. Architecture

```
realtime.RealtimeService
 ├─ _realtime_loop (5 s)          # WS snapshots (GPU current draw rides here)
 ├─ _metrics_loop (5 s)           # GPU/system history + host electricity sample
 └─ GpuEnergySampler (0.5 s / 10 s)  # own task; RAM buffer → aggregated rows
```

* `webui/gpu_energy.py` — sampler + summaries; singleton
  `get_gpu_energy_sampler()`.
* `webui/electricity.py` — host sources, host kWh integration, GPU window
  summaries, tariff config; singleton `get_electricity_service()`.
* `webui/routers/electricity.py` — HTTP API below.
* Storage reuses the existing `metrics` table + `prune_old_metrics()`
  retention (`HISTORY_RETENTION`, default 3600 s) — **no schema change, no
  second telemetry mechanism**. Both `kind="electricity"` (host samples) and
  `kind="gpu_energy"` (aggregated points) live there.

## 6. HTTP API

All endpoints require auth (`require_user`); tariff write requires admin and
is rate-limited (`rate_limit_dangerous`). Never a 500 for “no data” —
structured payloads instead.

| Endpoint | Returns |
|---|---|
| `GET /api/electricity` | live host sample `{available, measured, source, watts, gpu_power:{current_watts, per_device[], note}, components:{cpu, platform}, reason, ts}` |
| `GET /api/electricity/gpu-energy` | GPU windows `{today, h24, month, tariff{...}, sampler:{running, last_error, sample_interval, aggregate_interval}}`; each window `{available, energy_wh, kwh, average_power_w, measured_seconds, points, gpus[], note, cost?+currency?+cost_note? or cost:null+cost_unavailable}`; `tariff` block: `{tariff, currency, tariff_is_configured, tariff_notice, current_power_w, current_cost_per_hour, average_power_w, average_cost_per_hour, cost_unavailable}` (projection/accumulated keys always present; `null` = unavailable) |
| `GET /api/electricity/gpu-energy-window?minutes=` | one arbitrary GPU window (selected period) + `window_label` + cost / `cost_unavailable` |
| `GET /api/electricity/gpu-history?minutes=` | aggregated `gpu_energy` points (chart feed) |
| `GET /api/electricity/summary?minutes=` | host kWh `{kwh, energy_basis, points, integrated_intervals, measured, method, tariff_configured, cost?, currency?}` |
| `GET /api/electricity/history?minutes=` | raw persisted host watts points (5 s cadence) |
| `GET/PUT /api/electricity/config` | tariff + currency (admin) |

## 7. Units

* Power: **watts** (W; sysfs µW / NVML mW converted in code).
* Energy: **Wh** internally, displayed as **kWh** (4 decimals).
* Tariff: **lei per kWh** (existing settings storage).
* Cost: configured currency (default label `lei`), always footnoted
  *“calculated: kWh × tariff”*.

## 8. UI (`webui/static/js/pages/electricity.js`)

* Total server power card — measured tiles or the honest
  **TOTAL SERVER — data unavailable** state with the reason.
* GPU energy card — the **TARIFF** banner (explicit value or
  *tariff not configured*), current W (from the realtime snapshot), average W
  (24 h), the **CURRENT COST / AVERAGE COST** projection tiles (lei/hour,
  footnoted), energy today / 24 h / 30 d, the **COST TODAY / COST 24H /
  COST 30D** accumulated tiles, measured-seconds and energy-basis tiles,
  per-GPU split, sampler state pill (running / stopped + error),
  7 d/30 d **SELECTED PERIOD** cards (with accumulated cost), and the
  60 min average-power chart.
* Tariff settings (admin): current tariff/currency shown explicitly,
  new-value input + Save; after saving, the tariff banner and all costs
  re-render immediately.
* One page-scoped 5 s poll (`setInterval`) cleared on re-render/navigation;
  the GPU energy block refreshes every ~30 s via the same timer — no second
  polling mechanism. The Dashboard only links here and runs no electricity
  polling of its own.

## 9. Limitations (v1.2)

* **GPU-only hosts show no total** — by design. GPU energy is real and shown,
  but the total server line stays unavailable until a host-level source
  exists.
* GPU energy windows longer than metrics retention (default 1 h) cover only
  the retained tail; `measured_seconds` makes the coverage explicit. Raise
  `HISTORY_RETENTION` for month-scale windows.
* “Energy today” uses a UTC day boundary (retention permitting) — an
  approximation for local-time zones.
* Cost is a tariff-based estimate, exact only for flat tariffs; hourly cost
  figures are projections (the power may change within the hour); GPU cost
  covers GPU consumption only — never the whole server.
* RAPL package energy covers the CPU package (+ present sub-domains), not
  the wall draw; when RAPL and hwmon both exist the headline is their sum —
  an approximation, labeled measured because both inputs are measurements.
