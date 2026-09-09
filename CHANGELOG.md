# Changelog

## 1.5.2 (2026-11)

### Changed

- **Dashboard v3.2 — final Hardware Cards layout (dedup).** The four
  hardware cards are compact landscape cards with one identical structure:
  head (inline-SVG icon + name + ONE ring indicator on the right) → the
  SINGLE large percentage → (GPU only: device name) → kv row pinned to the
  card bottom. Removed all duplicated values: CPU's `load x / n threads`
  sub-line and model line; RAM's and Storage's duplicated `used / total`
  sub-lines; GPU's `utilization` label, ring cap (`x / y GB`), `Model VRAM
  (Ollama)` cell, `Fan PWM` (merged into one `Fan`), sysLine and
  `Clocks`. The GPU kv row is now POWER / TEMP / VRAM (used/total, NVML) /
  FAN / **CPU-GPU** — the new split aggregates the SAME `/api/ps` running
  list already used by the Running table (`sum(size_vram)/sum(size)`,
  e.g. `35% / 65%`; full GPU offload renders `0% / 100%`; when no model
  reports both fields or Ollama is offline it honestly renders `—`, never
  an invented number). Rings are pure indicators now (no percentage text
  inside; value on hover), so the main percentage is the only large
  numeric value. Layout: desktop 4 cards per row / tablet 2 / mobile 1;
  uniform card height (`min-height`, kv `margin-top: auto`); the kv row
  stays horizontal on desktop and wraps only under 480px. UI-only —
  backend, API, telemetry, realtime/WebSocket, polling, Electricity and
  NVMe untouched; `drawGpu` now also receives the SAME `snap.ollama` the
  Running table already uses (no new sampler, no new request). Tests:
  `tests/test_dashboard_v3_frontend.py` extended to 22 jsdom cases +
  2 new python source tests (dedup contract, one-ring-per-card,
  4/2/1 responsive CSS contract); zero/offline semantics preserved.
  Docs: `docs/DASHBOARD-V3.md` §"v3.2".

## 1.5.1 (2026-11)

### Changed

- **Dashboard v3.1 — UI polish: professional SVG icons + hardware card
  composition.** All emoji on the Dashboard (hardware card heads, top
  tiles, quick-nav links, empty states) are replaced with a single set of
  monochrome inline-SVG outline icons (17px, `stroke="currentColor"`,
  1.6 stroke, round caps — `iconSvg()` in `dashboard.js`, no icon font,
  no external library, no extra request): CPU = chip, RAM = memory
  module, GPU = graphics card, Storage = disk, Ollama = server, Models =
  layers, Running = play, Jobs = activity, Electricity = power bolt. The
  hardware cards (RAM/GPU/Storage) now share one fixed composition:
  main metric LEFT, circular indicator RIGHT, full-width progress bar
  below, then USED/FREE/TOTAL in ONE horizontal row (flex with equal
  cells; wraps to stacked pairs under 480px). CPU keeps its logically
  matching structure (utilization + ring, optional load bar,
  Cores/Threads/Frequency row — no faked storage-style values). UI-only:
  no backend/API/telemetry/realtime/polling/value changes, Electricity
  untouched, still no NVMe. Tests: 2 new regression scenarios in
  `tests/test_dashboard_v3_frontend.py` (no emoji anywhere + uniform SVG
  icon markup incl. per-slot glyphs; horizontal card structure incl.
  responsive kv fallback) — 20 cases total. Docs: `docs/DASHBOARD-V3.md`
  §"v3.1 UI polish".

## 1.5.0 (2026-11)

### Changed

- **Dashboard v3 — visual redesign + REAL hardware metrics**: the Dashboard
  is rebuilt around (1) a top nav row of 5 tiles (Ollama / Models / Running /
  Jobs / Electricity) + quick links, (2) four hardware cards — CPU (real
  load, model name from `/proc/cpuinfo`, cores/threads/load/frequency), RAM
  (used/free/total + %), GPU (the SAME single-source realtime snapshot:
  utilization ring, VRAM used/total ring, temperature, power, Model-VRAM
  (Ollama) as a SEPARATE labelled value, multi-GPU mini-rows never
  collapsed), Storage (disk aggregate from the same SystemCollector
  snapshot; NO NVMe/SMART/temperature) — and (3) a Running Models table in
  the style of `ollama ps` (Model / ID / Size / Processor / VRAM / Context
  / Duration / Status / Actions with Chat+Stop reusing the Running page
  handlers). Zero-value contract enforced everywhere: `0%`/`0 B`/`0 W`/
  `0 GB` render as the values they are, `100%` as `100%`; missing data
  renders as `—`, never as a fabricated zero. Ollama offline: the running
  table explicitly reports "Ollama offline — running models unknown" (no
  fake rows) and the Running count shows `—` (unknown ≠ 0). Loading /
  API-error / partial-snapshot / stale-PROCESSOR states preserved and
  extended; seq-guards prevent stale API answers from overwriting newer
  state. No new samplers, no new polling: the 3 s PROCESSOR poll remains
  the ONLY interval (stopped on every re-render), live updates ride the
  existing WebSocket snapshot, the Electricity tile keeps its single fetch
  per render + 60 s WS-cadence throttle.
- **Backend (same single-source architecture)**: `SystemCollector` now
  reports `cpu.model` (one-time `/proc/cpuinfo` read, validated, no
  subprocess); `GPUCollector.sample()` is single-flight (concurrent
  callers share ONE collection; the cache is only ever replaced by a
  completed one) and stamps explicit `ts` / `collected_at` / per-GPU `ts`;
  `RealtimeService` gives the Ollama status refresh a hard 3 s SLA so a
  hung llama-server can no longer freeze WebSocket snapshots (GPU/system
  start immediately, in parallel) and history records the snapshot's
  actual metric read time; `/api/status` serves the cached snapshot only
  while fresh (`REFRESH_INTERVAL + 1 s`) and otherwise builds live, so a
  stalled realtime loop cannot serve stale state forever.
- **Tests**: new `tests/test_dashboard_v3_frontend.py` — 18 jsdom scenarios
  (full render, zero-value semantics incl. 100%, ollama-ps table columns /
  derived split / actions, partial snapshot preservation, GPU-unavailable,
  offline, API error, Electricity states, WS live update, exactly-one-timer,
  overlapping-render race, snapshot cache, stale PROCESSOR after 7 s,
  Electricity 60 s throttle + force refresh, nav links, responsive markup)
  plus a source-level polling-discipline test; new
  `tests/test_gpu_stale_regression.py` (single-flight collection,
  timestamp semantics, hung-Ollama SLA, offline-not-crash, history ts,
  stale-cache rejection at `/api/status`). Removed
  `tests/test_dashboard_frontend.py` (v2-layout flows, incompatible with
  the v3 markup); its still-valid scenarios were ported to the v3 file.
  Docs: `docs/DASHBOARD-V3.md`.

## 1.4.3 (2026-09-08)

### Changed

- **Dashboard Electricity summary tile (v1.3)**: the Dashboard ELECTRICITY
  tile now shows a compact summary from the EXISTING
  `/api/electricity/gpu-energy` endpoint — with a configured tariff:
  energy today (X.XXXX kWh) · cost today (X.XX lei) · current cost
  (X.XXX lei / hour); tariff unset: kWh + "tariff not configured" (never a
  zero cost); no GPU energy telemetry: "data unavailable"; API error:
  explicit "unavailable" (never rendered as no data). The tile is fetched
  once per Dashboard render and softly refreshed riding the EXISTING
  websocket cadence with a 60 s throttle — no Dashboard-owned sampler, no
  new endpoint, no new polling timer; the 0.5 s NVML → RAM → 10 s
  aggregated pipeline is untouched and GPU numbers are never presented as
  total server energy. Tests: dashboard flows extended with electricity
  tile states (data/tariff/unset/no-telemetry/error/no-duplicate-polling);
  also fixed a pre-existing harness flaw where the race-condition check
  deadlocked on a per-call /api/status mock, silently skipping all later
  checks (now a shared promise — the whole 22-check suite actually runs).
  Docs: `docs/ELECTRICITY-V1.md` §8.1 (Dashboard tile contract).

## 1.4.2 (2026-09-08)

### Changed

- **Electricity v1.2 — tariff & cost clarity in the UI**: the GPU energy card
  now shows an explicit **TARIFF: X.XX lei / kWh** banner (or a
  "tariff not configured" notice — never an invented `0 lei`), two clearly
  labeled **projection** tiles — **CURRENT COST** (`current_power_w × tariff
  / 1000`, "if sustained 1 h") and **AVERAGE COST** (24 h `average_power_w ×
  tariff / 1000`) in lei/hour — and **accumulated** **COST TODAY / COST 24H /
  COST 30D** tiles (`measured kWh × tariff`, footnoted "accumulated:
  measured energy × tariff"), plus accumulated cost on the selected-period
  cards. Costs are computed in the API (`GET /api/electricity/gpu-energy`
  now carries a stable `tariff` block; projection keys always present,
  `null` = unavailable) and re-render immediately after the tariff is saved
  in Tariff settings. Hourly costs render at 3 decimals, accumulated at 2
  (API stores 4-decimal values). The 0.5 s NVML → RAM → 10 s aggregated
  pipeline, the GPU-only honesty notes and the compact Dashboard link are
  unchanged; no NVMe anywhere. Tests: tariff display, current/average cost
  per hour, accumulated cost, tariff-unset (no zeros), tariff change flow,
  rounding, GPU ≠ total — backend + frontend (34 electricity tests total).
  Docs: `docs/ELECTRICITY-V1.md` rewritten for v1.2.

## 1.4.1 (2026-09-08)

### Changed

- **Electricity v1.1 — GPU energy sampling pipeline + NVMe removal**: GPU
  `power_draw` is now sampled every **0.5 s** via NVML into a bounded
  **RAM-only buffer** (no DB writes, no disk I/O at that cadence); every
  **10 s** the buffer is aggregated into **one** DB point per GPU —
  `{gpu_index, interval_seconds, average_power_w, energy_wh, source: "NVML"}`
  with `energy_wh = average_power_w × interval_seconds / 3600` self-consistent
  against the stored values. Gaps (NVML down, sampler stopped) produce no
  rows and are never zero-filled or interpolated; rows carry their own
  measured interval and are time-disjoint, so restarts cannot double-count;
  `stop()` flushes the last actually-measured partial interval. The sampler
  is a singleton (idempotent `start()`, no duplicate tasks) started/stopped
  with the realtime service. New endpoints `/api/electricity/gpu-energy`
  (today / 24 h / month windows + sampler state), `/gpu-energy-window`
  (selected period) and `/gpu-history` (chart feed). UI: GPU block with
  current W, average W, energy today/24h/period/30d, per-GPU split,
  measured-seconds, cost (only with a configured tariff, labeled
  "calculated") — always labeled GPU-only, never total server power;
  TOTAL SERVER stays unavailable without host-level telemetry (verified on
  this host: RAPL denied even for uid 0, no hwmon power sensors). All NVMe
  telemetry/temperature support is removed from the project (no collector,
  no API/UI/tests/docs); drive-level sensors are excluded from host power
  probing. 29 electricity tests (backend + frontend flows: 0.5 s sampling,
  10 s aggregation, variable/idle/high power, energy formula, gaps, restart
  no-double-count, RAM-buffer-only writes, GPU ≠ total, NVML unavailable,
  tariff, no duplicate sampler). See `docs/ELECTRICITY-V1.md`.

## 1.4.0 (2026-09-08)

### Features

- **Electricity v1 — server power & cost tracking (local host only)**:
  new ⚡ Electricity page (linked from the sidebar and as a compact
  Dashboard card — the Dashboard itself runs no electricity polling)
  showing measured total server power (W) from host-level sources only —
  RAPL `energy_uj` (ΔJ/Δt) or hwmon `power*_input` — with GPU `power_draw`
  (NVML via the existing cached GPUCollector) shown strictly as a
  labelled reference that is **never** presented as total server power.
  Energy (kWh) is calculated by trapezoidal integration of the persisted
  watts series with restart/gap safety (intervals > 600 s excluded — no
  double-counting); cost = kWh × tariff appears only after an admin
  explicitly configures the tariff (no invented default — a
  "needs configuration" notice is shown instead). Samples persist into the
  existing `metrics` table (kind=`electricity`) on the existing 5 s
  metrics loop with 60 s source-probe caching — no second telemetry
  mechanism, no SSH/remote agents, no sudo, no per-request subprocesses.
  New API under `/api/electricity` (live sample, history, summary with
  kWh/cost clearly separated as calculated, tariff config GET/PUT,
  admin-protected). Explicit loading/unavailable/empty/error states
  everywhere; on hosts without a host-level source the page honestly says
  "Total server power: data unavailable". See `docs/ELECTRICITY-V1.md`.

## 1.3.0 (2026-09-07)

### Fixed

- **Models UI — model sizes now match `ollama ls`**: the shared byte
  formatter divided by 1024 (GiB) but labelled the result "GB", so a
  6_700_000_000-byte model showed "6.2 GB" while `ollama ls` showed
  "6.7 GB". `fmtBytes` now uses decimal units (bytes / 1_000) — the same
  units as the Ollama CLI — with the existing precision/rounding
  preserved; API values untouched. The fix applies everywhere the shared
  formatter renders sizes (Models, Running, Dashboard/GPU, PROCESSOR,
  System/Jobs speeds). See `docs/MODELS-SIZE-DISPLAY.md`.

### Features

- **Dashboard v2 — single daily-driver screen**: shell-first render with
  explicit loading/error/offline/empty states (API errors never render as
  "no data"), quick-nav to Models/Running/Jobs/Chat/Agents/GPU (tiles are
  links), compact Running block with model VRAM + CPU/GPU split badge from
  real data only, GPU VRAM vs Model VRAM shown as separate labelled rows
  from the ONE shared realtime snapshot, PROCESSOR poll fixed to a single
  page-scoped interval with seq-guards (no timer leaks, no duplicate
  polling, stale badge after repeated failures). No new backend APIs.
  See `docs/DASHBOARD-V2.md`.
- **Running v2 — model cards + live states**: per-model cards (model VRAM
  from `/api/ps` size_vram — not GPU telemetry, derived CPU/GPU split
  badge, context, unload time), explicit loading/error/offline/empty
  states, Chat deep link (`#chat?model=…` preselect), Stop with
  double-click guard + flight label, working refresh. Live updates still
  via the single realtime WS snapshot — no second polling mechanism.
  See `docs/RUNNING-UI-V2.md`.
- **Models v2 — Ollama capabilities**: model list shows Tools/Thinking/
  Completion/Vision chips (from Ollama `/api/tags`, never guessed),
  multi-select AND capability filter, running status column, Run/Stop
  row actions (`POST /api/ollama/models/{name}/run` → empty-prompt
  generate preload), honest offline/empty/filtered-empty states,
  in-place list refresh after delete. Unknown capabilities render marked
  and never match known filters. See `docs/MODELS-UI-V2.md`.
- `/api/ollama/models/{name}/show` surfaces `context_length` from
  `model_info` (real Ollama keeps it there, not in `details`).

## 1.2.0 (2026-09-07)

### Features

- **Chat v3 — Markdown rendering**: model replies render as Markdown
  (headers, bold/italic, lists, quotes, tables, inline/fenced code with
  `language-*` classes) via vendored marked 12.0.2 + DOMPurify 3.1.6
  (`js/md.js` bridge). Sanitization is mandatory in the pipeline: scripts,
  event handlers, `javascript:` URLs and embeds are stripped; the bridge
  degrades to escaped plain text if a library is missing. See
  `docs/CHAT-UI-V3.md`.

## 1.1.0 (2026-09-06)

### Features

- **Chat v2** — streaming chat UI: tokens render incrementally via the new
  `POST /api/chat/stream` SSE endpoint (Ollama NDJSON → SSE proxy),
  Send ↔ Stop toggle (AbortController), conversation history kept in state,
  generation cursor, in-transcript error rendering, completion metrics
  (model / tokens / duration), Clear button. `POST /api/chat/run` kept for
  compatibility. See `docs/CHAT-UI-V2.md`.

## 1.0.0 (2026-08-28)

Initial release.

### Features

- **Dashboard** — real-time GPU, Ollama, CPU overview with 10 history charts
- **Model Manager** — list, search, filter, sort, multi-select, batch delete, show (full details), copy, delete, pull (streaming progress), push, create (visual + raw Modelfile editor)
- **Running Models** — live list with VRAM, unload via `keep_alive=0`
- **Jobs** — pull/push/create/delete/copy/stop as async jobs, WebSocket progress, SQLite persistence, cancellation, restart recovery
- **System Monitoring** — CPU, RAM, swap, disk, network, top processes, Ollama process
- **GPU Page** — full NVML/nvidia-smi data, GPU processes, 16 GB VRAM tracking
- **V100 Fan Control** — reads fan_target/fan_pwm from `v100-fan.service` (ESP32 controller), never controls PWM
- **Ollama Console** — whitelisted commands, shell-injection protection, API translation + local CLI
- **LLM API** — manage OpenAI-compatible endpoints (Ollama, OmniRouter, +custom), status, latency, models, API key masking, SSRF protection
- **Logs** — Web UI, job, Ollama (systemd) logs — tail/search/filter
- **Settings** — configuration, password change, user management, audit log
- **Security** — PBKDF2 auth, sessions, CSRF, rate limiting, command whitelist, no-shell subprocess, audit log, API key masking, SSRF
- **WebSocket** — real-time snapshot push every 1s, job updates
- **History** — GPU/system metrics persisted to SQLite, pruned automatically, 10 chart types

### Architecture

- **Backend**: Python 3.12+, FastAPI, Uvicorn, psutil, httpx, aiosqlite, nvidia-ml-py
- **Frontend**: Vanilla JS SPA, Chart.js 4.4, hash router, WebSocket
- **Storage**: SQLite (settings, jobs, metrics, users, audit_log)
- **GPU**: NVML (primary) + nvidia-smi (fallback)
- **Fan**: `v100-fan.service` journalctl reader (systemd-journal group)
- **Deployment**: systemd unit + Docker compose

### Tests

- 83+ pytest tests (mock mode, no GPU required)
- jsdom frontend smoke test (all pages, auth, rendering)
- bash syntax validation