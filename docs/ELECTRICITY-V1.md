# Electricity v1 — server power & cost tracking

**Status:** implemented (v1) · **Scope:** the LOCAL machine running WebOllama only.

Electricity is a separate page (`#electricity`, ⚡ in the sidebar, linked as a
compact card from the Dashboard) that tracks the server's power draw,
consumption and — with an explicitly configured tariff — estimated cost.
It reuses the existing metrics storage and the existing GPU collector; there
is **no second telemetry mechanism, no SSH, no remote agent, no sudo and no
per-request subprocesses**.

---

## 1. What is measured vs calculated (the core contract)

| Value | Kind | Source of truth |
|---|---|---|
| **Total server power (W)** | **MEASURED** — only when a host-level source exists | RAPL `energy_uj` (ΔJ/Δt) or hwmon `power*_input` |
| **GPU power draw (W)** | MEASURED, **reference only** | NVML `power_draw` via the existing `GPUCollector` |
| **Energy (kWh)** | **CALCULATED** | Trapezoidal integration of the measured watts series |
| **Cost** | **CALCULATED** | `kWh × tariff` — only with a configured tariff |

Hard rules enforced in code and UI:

1. **GPU power is never presented as total server power.** The headline
   `watts` is non-null only if RAPL or hwmon produced a reading. On a
   GPU-only host the page says *“Total server power: data unavailable”* and
   shows the GPU number under an explicit *“GPU draw (reference) — not total
   server power”* label.
2. **No invented tariff.** Until the admin saves a tariff, cost is never
   computed and a *“tariff not configured”* notice is shown instead. There
   is no default lei/kWh value.
3. **No fabricated readings.** If no source is readable, every value shows
   `—` / *unavailable* with the reason; nothing is extrapolated from
   temperature, load or GPU power.

## 2. Data sources (probed in order)

| Source | Sysfs / API path | Units | Host-level total? | Notes |
|---|---|---|---|---|
| RAPL | `/sys/class/powercap/intel-rapl:*/energy_uj` | µJ counter → W via ΔJ/Δt | **yes** (CPU package + sub-domains) | Root-only (0400) on many kernels/containers — denial is a normal, structured outcome. Wrap/reset guard: Δ outside `[0, 1e6 J)` or Δt outside `(0, 60 s)` is discarded. First sample after probe reports no watts (a Δ needs two points). |
| hwmon | `/sys/class/hwmon/hwmon*/power*_input` | µW | **yes** (platform sensors) | Chips named `drivetemp` are excluded — drive sensors are not system power. |
| NVML | existing `GPUCollector` (`power_draw` per GPU) | W | **no — GPU-only reference** | Reuses the collector's 0.9 s TTL cache; zero extra NVML/subprocess polling. |

Source discovery (which `hwmon*` dirs exist, which RAPL domains are
readable) is cached for **60 s** (`PROBE_INTERVAL`) — repeated samples do
not re-scan sysfs. The per-sample reads themselves are a handful of tiny
file reads; the whole service is designed around the 5 s metrics loop.

## 3. Architecture

```
realtime._metrics_loop (5 s)
 ├─ build_snapshot(persist=True)      # existing GPU/system history
 └─ ElectricityService.record()       # one compact sample → metrics(kind="electricity")
      ├─ RaplSource.sample()          #   ΔJ/Δt or None
      ├─ HwmonPowerSource.sample()    #   Σ power*_input
      └─ gpu.sample() (cached)        #   GPU reference watts
```

* `webui/electricity.py` — sources + `ElectricityService` (sample / record /
  energy_summary / config), singleton `get_electricity_service()`.
* `webui/routers/electricity.py` — HTTP API below.
* Persisted payload (metrics row, kind=`electricity`):
  `{watts, gpu_watts, measured, source}` — only written when a host-level
  reading exists (unavailable hours simply have no rows, and are never
  interpolated).
* Storage reuses the existing `metrics` table + `prune_old_metrics()`
  retention (`HISTORY_RETENTION`, default 3600 s) — no schema change.
* Restart handling: the service keeps no in-memory energy accumulator; kWh
  is always recomputed from persisted rows, and any interval gap > 600 s
  (restart, source outage, pruning) is excluded from integration — no
  double-counting, no phantom consumption across restarts.

## 4. HTTP API

All endpoints require auth (`require_user`); tariff write requires admin
and is rate-limited (`rate_limit_dangerous`). Responses are always 200 with
structured payloads — `available: false` + `reason`, never a traceback.

| Endpoint | Returns |
|---|---|
| `GET /api/electricity` | live sample: `{available, measured, source, watts, gpu_power:{watts, per_device[], note}, components:{cpu, platform}, reason, ts}` |
| `GET /api/electricity/history?minutes=1..1440` | `{kind, minutes, points:[{ts, data:{watts,...}}], retention}` |
| `GET /api/electricity/summary?minutes=1..1440` | `{kwh, energy_basis, points, integrated_intervals, window_seconds, measured, method, tariff_configured, cost?, currency?, window_bounded_by_retention}` |
| `GET /api/electricity/config` | `{tariff, currency, tariff_is_configured, notice}` (admin) |
| `PUT /api/electricity/config` | body `{tariff?, currency?}` → updated config (admin) |

The API never merges the categories: `watts` (measured total), `gpu_power`
(measured GPU reference) and `kwh`/`cost` (calculated) are separate fields,
and `energy_basis` states explicitly how the kWh was derived
(`calculated-from-measured-power` vs
`calculated-from-partially-unavailable-power`).

## 5. Units

* Power: **watts** (W, one decimal in UI; sysfs µW/µJ converted in code).
* Energy: **kilowatt-hours** (kWh, 4 decimals in UI; internally Wh).
* Tariff: **lei per kWh** (stored via the existing settings mechanism).
* Cost: displayed in the configured currency symbol (default label `lei`),
  always footnoted *“calculated: kWh × tariff”*.

## 6. UI behaviour (`webui/static/js/pages/electricity.js`)

* Compact summary tiles: total, CPU (RAPL), platform (hwmon), GPU reference.
* Energy history chart (last 60 min) + period cards for **24 h / 7 d / 30 d**
  with kWh and cost; cards note when a window is capped by retention.
* Live section polls `GET /api/electricity` every **5 s** — one page-scoped
  `setInterval`, cleared on re-render/navigation (same discipline as the
  Dashboard's PROCESSOR poll; the Dashboard itself only links here and runs
  no electricity polling of its own).
* Explicit states everywhere: **loading**, **unavailable** (with reason +
  honest GPU reference), **empty** (no history yet / no complete intervals),
  **error** (API failure message), tariff **needs-configuration** notice.

## 7. Limitations (v1)

* **No GPU-only total:** on hosts where RAPL is denied and no hwmon power
  sensors exist (the current deployment host), *total server power is
  genuinely unavailable* — the page says so and only the GPU reference is
  measured. Verified on this host: `/sys/class/powercap/intel-rapl:0/energy_uj`
  is mode 0400 `nobody:nogroup` inside the container and returns EACCES
  **even for uid 0** (no perf-events / MSR privilege in the container);
  no hwmon chip exposes `power*_input`; `/sys/class/power_supply` is empty;
  no IPMI device or ipmitool exists; no NVML GPU is present on this
  development instance (the production GPU host would supply the GPU
  reference at minimum). Enabling RAPL (kernel boot param `intel_idle.max_cstate`/
  `msr`-permitted environments, or running outside a restricted container)
  or exposing a hwmon power sensor immediately lights the page up — no code
  changes needed.
* kWh windows longer than the metrics retention (default 1 h) report the
  largest window actually retained (`window_bounded_by_retention`); 24 h+
  cards therefore only cover the retained tail unless `HISTORY_RETENTION`
  is raised.
* Cost is a *tariff-based estimate*, not a utility-meter reading; it is
  exact only if the tariff is flat (no day/night zones, no standing charge).
* RAPL package energy covers the CPU package (+ sub-domains present in
  sysfs), not the whole socket wall draw; platform hwmon sensors vary by
  motherboard. When both exist the headline is their sum — an
  approximation, labeled measured because both inputs are measurements.
