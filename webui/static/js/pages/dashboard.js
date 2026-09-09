/* Dashboard v3 — visual redesign + REAL hardware metrics.
 *
 * Layout (mirrors the agreed mock-up, desktop-first, stacks on mobile):
 *   TOP NAV CARDS   [ Ollama ][ Models ][ Running ][ Jobs ][ Electricity ]
 *   HARDWARE        [ CPU ][ RAM ][ GPU ][ STORAGE ]
 *   RUNNING MODELS  ollama-ps-style table (Model/ID/Size/Processor/VRAM/
 *                   Context/Duration/Status/Actions)
 *   PROCESSOR       existing block (3s page-scoped poll, unchanged)
 *   HISTORY         existing charts (unchanged)
 *
 * Data sources — SINGLE-SOURCE TELEMETRY, unchanged architecture:
 *   CPU / RAM / Storage -> the shared realtime snapshot (SystemCollector /
 *      psutil inside RealtimeService.build_snapshot). No second collector,
 *      no df polling, no subprocess per card.
 *   GPU (util/temp/power/VRAM/driver/cuda) -> the same snapshot's gpu object
 *      (the ONE GPUCollector snapshot used by topbar, GPU page, PROCESSOR).
 *      GPU VRAM (NVML) is rendered strictly separately from model VRAM
 *      (ollama size_vram).
 *   Ollama running -> snap.ollama.running (OllamaClient -> /api/ps, the data
 *      behind `ollama ps`). Processor split is the EXISTING honest derivation
 *      from size vs size_vram and is labelled as derived, never as telemetry.
 *   Electricity -> existing /api/electricity/gpu-energy, fetched once per
 *      render + throttled WS-cadence refresh (v1.3 contract, unchanged).
 *
 * Live updates ride the EXISTING websocket snapshot — the only timer on this
 * page remains the PROCESSOR poll. An API error never renders as "no data".
 *
 * ZERO-VALUE SEMANTICS: 0 is a VALUE, not "unavailable". CPU 0% renders as
 * "0%", RAM/GPU utilization 0% as "0%", 100% as "100%", VRAM 0 as
 * "0 GB / total", power 0 as "0 W". Only null/undefined/NaN render as "—".
 */
(function () {
  let charts = null;
  let procTimer = null;
  let procSeq = 0;      // race guard: only the latest processor response wins
  let lastProcOk = 0;   // timestamp of the last successful processor poll

  /* ---- zero-safe formatters (v3 contract: zero is a value) ---- */
  function fmtPct(v) {
    if (v === null || v === undefined || v === "") return null;
    const n = Number(v);
    if (!Number.isFinite(n)) return null;
    return Math.round(n) + "%";
  }
  function fmtWatts(v) {
    if (v === null || v === undefined) return null;
    const n = Number(v);
    if (!Number.isFinite(n)) return null;
    return n.toFixed(1).replace(/\.0$/, "") + " W";
  }
  function fmtTempC(v) {
    if (v === null || v === undefined) return null;
    const n = Number(v);
    if (!Number.isFinite(n)) return null;
    return Math.round(n) + "°C";
  }
  function fmtGb(v) {
    if (v === null || v === undefined) return null;
    const n = Number(v);
    if (!Number.isFinite(n)) return null;
    return (n / 1e9).toFixed(1).replace(/\.0$/, "") + " GB";
  }
  function fmtMhz(v) {
    if (v === null || v === undefined) return null;
    const n = Number(v);
    if (!Number.isFinite(n) || n <= 0) return null;
    return (n / 1000).toFixed(2).replace(/\.?0+$/, "") + " GHz";
  }
  function fmtDur(sec) {
    if (sec === null || sec === undefined || !Number.isFinite(Number(sec))) return null;
    sec = Math.max(0, Math.floor(sec));
    const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
    if (h) return `${h}h ${m}m`;
    if (m) return `${m}m ${s}s`;
    return `${s}s`;
  }
  function remainingSec(iso) {
    if (!iso) return null;
    const t = Date.parse(iso);
    if (Number.isNaN(t)) return null;
    return Math.max(0, (t - Date.now()) / 1000);
  }
  function pctColor(p) {
    const n = Number(p);
    if (!Number.isFinite(n)) return "var(--accent)";
    if (n >= 90) return "var(--red)";
    if (n >= 60) return "var(--yellow)";
    return "var(--green)";
  }
  function orDash(v) { return v === null ? "—" : v; }

  /* ---- progress ring / bar (SVG keeps the DOM harness-friendly) ---- */
  function ringSvg(pct, label) {
    const p = Math.max(0, Math.min(100, Number(pct) || 0));
    const R = 26, C = 2 * Math.PI * R;
    const filled = (p / 100) * C;
    return `<svg class="hw-ring" viewBox="0 0 64 64" width="64" height="64" aria-hidden="true">
      <circle class="ring-bg" cx="32" cy="32" r="${R}"></circle>
      <circle class="ring-fg" cx="32" cy="32" r="${R}" style="stroke:${pctColor(p)}" stroke-dasharray="${filled.toFixed(2)} ${(C - filled).toFixed(2)}"></circle>
      <text class="hw-ring-text" x="32" y="36" text-anchor="middle">${label}</text>
    </svg>`;
  }
  function barHtml(pct, color) {
    const p = Math.max(0, Math.min(100, Number(pct) || 0));
    return `<div class="hw-bar"><div style="width:${p.toFixed(1)}%;background:${color || pctColor(p)}"></div></div>`;
  }

  /* ---- icons: monochrome inline SVG (no emoji on a monitoring dashboard) ----
   * One visual language: 1.6-stroke outline, round caps, 17px box, colored
   * by CSS `currentColor`. Pure markup — no icon font, no external library,
   * no extra request. Rendered inside .hw-icon / .tile-icon spans. */
  function iconSvg(name) {
    const paths = {
      // chip: square package + pins + inner die
      cpu: `<rect x="5.5" y="5.5" width="11" height="11" rx="1.5"/>
            <rect x="9" y="9" width="4" height="4" rx="0.8"/>
            <path d="M9 2.5v3M13 2.5v3M9 16.5v3M13 16.5v3M2.5 9h3M2.5 13h3M16.5 9h3M16.5 13h3"/>`,
      // memory module: stick + chips + notch row
      ram: `<rect x="2.5" y="4.5" width="17" height="9.5" rx="1"/>
            <path d="M2.5 14h17v3.5h-17z"/>
            <path d="M6 7.5v3M9.5 7.5v3M13 7.5v3M16.5 7.5v3"/>`,
      // graphics card: board + bracket + fan
      gpu: `<rect x="2.5" y="5.5" width="15" height="9" rx="1.5"/>
            <path d="M2.5 17.5h3M17.5 8h2v5h-2"/>
            <circle cx="8.5" cy="10" r="3"/>
            <path d="M13.5 8.5l2-1.5"/>`,
      // disk: drive body + platter + activity lines
      storage: `<rect x="2.5" y="5.5" width="17" height="11" rx="2"/>
            <circle cx="8" cy="11" r="2.2"/>
            <path d="M13 9h4M13 13h4"/>`,
      // server: rack units with status LEDs
      ollama: `<rect x="2.5" y="3.5" width="17" height="6" rx="1.5"/>
            <rect x="2.5" y="12.5" width="17" height="6" rx="1.5"/>
            <path d="M5.5 6.5h.01M8 6.5h.01M5.5 15.5h.01M8 15.5h.01"/>`,
      // layers: stacked models
      models: `<path d="M11 2.5 20 7l-9 4.5L2 7z"/>
            <path d="M4.2 9.8 2 11l9 4.5 9-4.5-2.2-1.2"/>
            <path d="M4.2 13.8 2 15l9 4.5 9-4.5-2.2-1.2"/>`,
      // play: loaded / running
      running: `<circle cx="11" cy="11" r="8.5"/>
            <path d="M9 7.5l6 3.5-6 3.5z"/>`,
      // activity: jobs / task pulse
      jobs: `<path d="M2.5 12h4l2.5-6.5 4 11 2.5-4.5h4"/>`,
      // bolt: power / electricity
      power: `<path d="M12.5 2.5 5 12.5h5l-1.5 8L17 10h-5z"/>`,
    };
    const d = paths[name];
    if (!d) return "";
    return `<svg class="icon" viewBox="0 0 22 22" width="17" height="17" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${d}</svg>`;
  }

  async function render(el) {
    Charts.destroyAll("dash");
    charts = null;
    stopProcessorPolling();               // no leaked timers across page switches
    procSeq++;
    const seq = procSeq;

    // Build the shell FIRST so the page has real loading states while
    // /api/status is in flight.
    el.innerHTML = buildPage(null, "");

    // Initial data: cached snapshot if present, else /api/status.
    let snap = null;
    let loadError = "";
    if (App.state.snapshot) {
      snap = App.state.snapshot;
    } else {
      try {
        snap = await API.get("/api/status");
        if (seq !== procSeq) return;      // superseded by a newer render
        App.state.snapshot = snap;
      } catch (e) {
        if (seq !== procSeq) return;
        loadError = e.message || "status unavailable";
        snap = null;
      }
    }

    el.innerHTML = buildPage(snap, loadError);
    wireStatic(snap);
    drawSnapshot(snap, loadError);
    if (snap) startCharts();
    loadHistory(60);
    startProcessorPolling();
    loadElectricitySummary(); // ONE existing-API fetch per render; NO new timer
  }

  /* ---- page shell ---- */
  function hwShell(icon, title, id) {
    return `<div class="hw-card" id="${id}">
      <div class="hw-card-head"><span class="hw-icon">${icon}</span><span class="hw-name">${title}</span></div>
      <div class="hw-loading">⏳ Loading…</div>
    </div>`;
  }

  function buildPage(snap, loadError) {
    const offline = !snap || !(snap.ollama && snap.ollama.online);
    return `
      ${loadError ? `<div class="alert error" id="dash-alert">⚠ Dashboard data unavailable — ${esc(loadError)}. <a href="#dashboard">Retry</a> or check the backend.</div>` : ""}

      <div class="dash-top-grid">
        <div class="metric-tile ${offline ? "bad" : "good"}" id="dash-ollama-tile">
          <div class="metric-label"><span class="tile-icon">${iconSvg("ollama")}</span>Ollama</div>
          <div class="metric-value" style="font-size:16px" id="dash-ollama-state">${offline && snap ? "● OLLAMA OFFLINE" : snap ? "● ONLINE" : "⏳ Loading…"}</div>
          <div class="metric-sub" id="dash-ollama-sub">${snap && snap.ollama ? `v${esc(snap.ollama.version || "—")} · ${esc(snap.ollama.endpoint || "")}` : "waiting for data…"}</div>
        </div>
        <a class="metric-tile dash-link" href="#models" id="dash-models-tile">
          <div class="metric-label"><span class="tile-icon">${iconSvg("models")}</span>Models</div>
          <div class="metric-value" id="dash-models-count">—</div>
          <div class="metric-sub" id="dash-models-sub">installed · open Models →</div>
        </a>
        <a class="metric-tile dash-link" href="#running" id="dash-running-tile">
          <div class="metric-label"><span class="tile-icon">${iconSvg("running")}</span>Running</div>
          <div class="metric-value" id="dash-running-count">—</div>
          <div class="metric-sub">loaded · open Running →</div>
        </a>
        <a class="metric-tile dash-link" href="#jobs" id="dash-jobs-tile">
          <div class="metric-label"><span class="tile-icon">${iconSvg("jobs")}</span>Jobs</div>
          <div class="metric-value" id="dash-jobs-count">—</div>
          <div class="metric-sub" id="dash-jobs-sub">recent · open Jobs →</div>
        </a>
        <a class="metric-tile dash-link" href="#electricity" id="dash-electricity-tile">
          <div class="metric-label"><span class="tile-icon">${iconSvg("power")}</span>Electricity</div>
          <div class="metric-value" style="font-size:14px" id="dash-electricity-value">—</div>
          <div class="metric-sub" id="dash-electricity-sub">power & cost · open Electricity →</div>
        </a>
      </div>

      <div class="dash-nav">
        <a class="btn-sm" href="#models">${iconSvg("models")} Models</a>
        <a class="btn-sm" href="#running">${iconSvg("running")} Running</a>
        <a class="btn-sm" href="#jobs">${iconSvg("jobs")} Jobs</a>
        <a class="btn-sm" href="#chat">${iconSvg("ollama")} Chat</a>
        <a class="btn-sm" href="#agents">${iconSvg("jobs")} Agents</a>
        <a class="btn-sm" href="#gpu">${iconSvg("gpu")} GPU</a>
      </div>

      <div class="dash-section-title">Hardware</div>
      <div class="hw-grid">
        ${hwShell(iconSvg("cpu"), "CPU", "dash-hw-cpu")}
        ${hwShell(iconSvg("ram"), "RAM", "dash-hw-ram")}
        ${hwShell(iconSvg("gpu"), "GPU", "dash-hw-gpu")}
        ${hwShell(iconSvg("storage"), "Storage", "dash-hw-storage")}
      </div>

      <div class="dash-section-title" style="margin-top:18px">Running Models <span class="ps-src">(ollama ps)</span>
        <span class="right" style="margin-left:auto"><a class="btn-sm" href="#running">open Running →</a></span></div>
      <div class="card" style="padding:6px 12px 10px">
        <div id="dash-running"><div class="empty text-dim">⏳ Loading…</div></div>
      </div>

      <div class="card" style="margin-top:4px">
        <div class="card-title">PROCESSOR <span class="right"><span class="proc-host" id="proc-host"></span><span class="text-faint" id="dash-proc-state"></span></span></div>
        <div id="proc-body" class="proc-unavailable">⏳ Loading processor data…</div>
      </div>

      <div class="card">
        <div class="card-title">History · <span style="text-transform:none" id="dash-range-label">1 hour</span>
          <span class="right">
            <button class="btn-sm dash-range" data-min="1">1m</button>
            <button class="btn-sm dash-range" data-min="5">5m</button>
            <button class="btn-sm dash-range" data-min="15">15m</button>
            <button class="btn-sm dash-range active" data-min="60">1h</button>
          </span>
        </div>
        <div class="grid grid-2" id="dash-charts"></div>
      </div>`;
  }

  function wireStatic(snap) {
    document.querySelectorAll(".dash-range").forEach((btn) => {
      btn.onclick = () => {
        document.querySelectorAll(".dash-range").forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
        loadHistory(Number(btn.dataset.min));
        document.getElementById("dash-range-label").textContent =
          btn.dataset.min === "1" ? "1 minute" : btn.dataset.min === "5" ? "5 minutes" : btn.dataset.min === "15" ? "15 minutes" : "1 hour";
      };
    });
  }

  /* ---- snapshot rendering (also used by onSnapshot live updates) ----
   * Section guards: a snapshot that lacks a section (temporarily
   * unavailable) leaves that block's last rendered state untouched —
   * partial data never wipes known-good UI. */
  function drawSnapshot(snap, loadError) {
    if (!snap) return; // error banner is already in the shell
    if (snap.ollama) {
      const ollama = snap.ollama;
      const oState = document.getElementById("dash-ollama-state");
      const oSub = document.getElementById("dash-ollama-sub");
      if (oState) {
        oState.textContent = ollama.online ? "● ONLINE" : "● OLLAMA OFFLINE";
        const oTile = document.getElementById("dash-ollama-tile");
        if (oTile) oTile.classList.toggle("bad", !ollama.online);
        if (oSub) oSub.textContent = `v${ollama.version || "—"} · ${ollama.endpoint || ""}`;
      }
      // Models / Running tiles (counts from the same snapshot)
      const mc = document.getElementById("dash-models-count");
      if (mc) mc.textContent = ollama.models_count != null ? String(ollama.models_count) : "—";
      const rc = document.getElementById("dash-running-count");
      if (rc) rc.textContent = ollama.running_count != null ? String(ollama.running_count) : (ollama.online ? "0" : "—");
      // ollama offline: the count is UNKNOWN, not zero — show the honest "—"
      if (rc && !ollama.online) rc.textContent = "—";
      drawRunning(snap);
    }
    if (snap.jobs) drawJobsTile(snap.jobs);
    if (snap.cpu) drawCpu(snap.cpu);
    if (snap.ram) drawRam(snap.ram);
    if (snap.gpu) drawGpu(snap.gpu);
    if (snap.disk) drawStorage(snap.disk);
  }

  /* ---- TOP: Jobs tile (counts only; the workflow lives on Jobs page) ---- */
  function drawJobsTile(jobs) {
    const el = document.getElementById("dash-jobs-count");
    if (!el) return;
    const list = Array.isArray(jobs) ? jobs : [];
    const active = list.filter((j) => j && j.status === "running").length;
    el.textContent = String(active);
    const sub = document.getElementById("dash-jobs-sub");
    if (sub) sub.textContent = `${active} active · ${list.length} recent · open Jobs →`;
  }

  /* ---- HARDWARE: CPU card (SystemCollector via the shared snapshot) ---- */
  function drawCpu(cpu) {
    const el = document.getElementById("dash-hw-cpu");
    if (!el) return;
    const pct = fmtPct(cpu.percent);                       // 0 → "0%", 100 → "100%"
    const load = [cpu.load_1, cpu.load_5, cpu.load_15].map((x) => (x == null ? null : Number(x)));
    const threads = cpu.threads != null ? Number(cpu.threads) : null;
    const loadRatio = load[0] != null && threads ? Math.min(100, (load[0] / threads) * 100) : null;
    const freq = cpu.frequency && cpu.frequency.current != null ? fmtMhz(cpu.frequency.current) : null;
    el.innerHTML = `
      <div class="hw-card-head"><span class="hw-icon">${iconSvg("cpu")}</span><span class="hw-name">CPU</span></div>
      <div class="hw-model-line" title="${esc(cpu.model || "")}">${cpu.model ? esc(cpu.model) : ""}</div>
      <div class="hw-main">
        <div class="hw-pct-block">
          <div class="hw-pct" id="dash-cpu-pct">${orDash(pct)}</div>
          <div class="hw-pct-sub" id="dash-cpu-load">${load[0] != null ? `load ${load[0].toFixed(2)}${threads ? ` / ${threads} threads` : ""}` : "—"}</div>
        </div>
        <div class="hw-ring-wrap">${ringSvg(cpu.percent, orDash(pct))}</div>
      </div>
      ${loadRatio != null ? barHtml(loadRatio) : ""}
      <div class="hw-kv">
        <div><div class="k">Cores</div><div class="v" id="dash-cpu-cores">${cpu.cores != null ? esc(String(cpu.cores)) : "—"}</div></div>
        <div><div class="k">Threads</div><div class="v" id="dash-cpu-threads">${threads != null ? esc(String(threads)) : "—"}</div></div>
        <div><div class="k">Frequency</div><div class="v" id="dash-cpu-freq">${orDash(freq)}</div></div>
      </div>`;
  }

  /* ---- HARDWARE: RAM card (SystemCollector / psutil — never model sizes) ---- */
  function drawRam(ram) {
    const el = document.getElementById("dash-hw-ram");
    if (!el) return;
    const pct = fmtPct(ram.percent);                       // 0 → "0%", 100 → "100%"
    el.innerHTML = `
      <div class="hw-card-head"><span class="hw-icon">${iconSvg("ram")}</span><span class="hw-name">RAM</span></div>
      <div class="hw-main">
        <div class="hw-pct-block">
          <div class="hw-pct">${orDash(pct)}</div>
          <div class="hw-pct-sub" id="dash-ram-sub">${ram.used != null && ram.total != null ? `${fmtBytes(ram.used)} / ${fmtBytes(ram.total)}` : "—"}</div>
        </div>
        <div class="hw-ring-wrap">${ringSvg(ram.percent, orDash(pct))}</div>
      </div>
      ${ram.total ? barHtml(ram.percent) : ""}
      <div class="hw-kv">
        <div><div class="k">Used</div><div class="v" id="dash-ram-used">${ram.used != null ? fmtBytes(ram.used) : "—"}</div></div>
        <div><div class="k">Free</div><div class="v" id="dash-ram-free">${ram.free != null ? fmtBytes(ram.free) : "—"}</div></div>
        <div><div class="k">Total</div><div class="v" id="dash-ram-total">${ram.total != null ? fmtBytes(ram.total) : "—"}</div></div>
      </div>`;
  }

  /* ---- HARDWARE: GPU card — the ONE shared snapshot (GPUCollector).
   * GPU utilization ≠ VRAM: utilization is the big number, VRAM has its
   * own ring. Model VRAM (Ollama size_vram) is a separate metric and is
   * always labelled as such. Multi-GPU: extra rows, devices never collapse. */
  function drawGpu(gpu) {
    const el = document.getElementById("dash-hw-gpu");
    if (!el) return;
    if (!gpu.available) {
      // explicit unavailable — GPU-less hosts are NORMAL, not an error
      el.innerHTML = `
        <div class="hw-card-head"><span class="hw-icon">${iconSvg("gpu")}</span><span class="hw-name">GPU</span></div>
        <div class="hw-loading" style="padding:6px 0 2px">${iconSvg("gpu")} ${esc(gpu.reason || "No NVIDIA GPU detected on this host")}</div>
        <div class="hw-foot">CPU-only Ollama works fine — this card fills in automatically when an NVIDIA GPU is present.</div>`;
      return;
    }
    const gpus = gpu.gpus || [];
    const g = gpus[0] || null;
    if (!g) {
      el.innerHTML = `
        <div class="hw-card-head"><span class="hw-icon">${iconSvg("gpu")}</span><span class="hw-name">GPU</span></div>
        <div class="hw-loading">GPU available, no devices reported — check the GPU page for details.</div>`;
      return;
    }
    const utilPct = fmtPct(g.utilization);
    const vramPct = g.vram_total ? (g.vram_used / g.vram_total) * 100 : null;
    const modelVram = gpu.ollama_vram && gpu.ollama_vram.total_vram;
    const sysLine = [g.cuda_version ? `CUDA ${esc(g.cuda_version)}` : (gpu.cuda_version ? `CUDA ${esc(gpu.cuda_version)}` : null),
                     (g.driver_version || gpu.driver_version) ? `driver ${esc(g.driver_version || gpu.driver_version)}` : null
                    ].filter(Boolean).join(" · ");
    const extra = gpus.slice(1).map((x) => {
      const vp = x.vram_total ? ((x.vram_used / x.vram_total) * 100).toFixed(0) + "%" : "—";
      return `<div class="hw-mini-gpu">#${esc(String(x.index))} ${esc(x.name || "GPU")} — ${orDash(fmtPct(x.utilization))} · ${orDash(fmtGb(x.vram_used))} / ${orDash(fmtGb(x.vram_total))} (${vp}) · ${orDash(fmtTempC(x.temperature))}</div>`;
    }).join("");
    el.innerHTML = `
      <div class="hw-card-head"><span class="hw-icon">${iconSvg("gpu")}</span><span class="hw-name">GPU</span></div>
      <div class="hw-model-line" title="${esc(g.name || "")}">${esc(g.name || "GPU")}</div>
      <div class="hw-main">
        <div class="hw-pct-block">
          <div class="hw-pct" id="dash-gpu-pct">${orDash(utilPct)}</div>
          <div class="hw-pct-sub">utilization</div>
        </div>
        <div class="hw-ring-wrap">${ringSvg(vramPct, vramPct != null ? Math.round(vramPct) + "%" : "—")}
          <div class="hw-ring-cap" id="dash-gpu-vram-cap">${g.vram_used != null && g.vram_total != null ? `${fmtGb(g.vram_used)} / ${fmtGb(g.vram_total)}` : "—"}</div>
        </div>
      </div>
      ${g.vram_total ? barHtml(vramPct) : ""}
      <div class="hw-kv">
        <div><div class="k">Power</div><div class="v" id="dash-power">${orDash(fmtWatts(g.power_draw))}</div></div>
        <div><div class="k">Temperature</div><div class="v" id="dash-temp">${orDash(fmtTempC(g.temperature))}</div></div>
        <div><div class="k">Model VRAM (Ollama)</div><div class="v" id="dash-modelvram" title="sum of running models' size_vram from /api/ps — not GPU telemetry">${modelVram ? fmtGb(modelVram) : "—"}</div></div>
        <div><div class="k">Fan Target</div><div class="v">${g.fan_available && g.fan_target != null ? Math.round(g.fan_target) + "%" : "—"}</div></div>
        <div><div class="k">Fan PWM</div><div class="v">${g.fan_available && g.fan_pwm != null ? Math.round(g.fan_pwm) + "%" : "—"}</div></div>
        <div><div class="k">Clocks</div><div class="v">${g.clocks != null ? g.clocks + " MHz" : "—"}</div></div>
      </div>
      ${sysLine ? `<div class="hw-foot" id="dash-gpu-sys">${sysLine}</div>` : ""}
      ${extra ? `<div class="hw-mini-gpus">${extra}</div>` : ""}`;
  }

  /* ---- HARDWARE: Storage card (SystemCollector disk aggregate — no df
   * polling, no new collector: the snapshot already samples it) ---- */
  function drawStorage(disk) {
    const el = document.getElementById("dash-hw-storage");
    if (!el) return;
    if (!disk || !disk.total) {
      el.innerHTML = `
        <div class="hw-card-head"><span class="hw-icon">${iconSvg("storage")}</span><span class="hw-name">Storage</span></div>
        <div class="hw-loading">${iconSvg("storage")} Storage data unavailable</div>`;
      return;
    }
    const pct = fmtPct(disk.percent);
    el.innerHTML = `
      <div class="hw-card-head"><span class="hw-icon">${iconSvg("storage")}</span><span class="hw-name">Storage</span></div>
      <div class="hw-main">
        <div class="hw-pct-block">
          <div class="hw-pct">${orDash(pct)}</div>
          <div class="hw-pct-sub" id="dash-disk-sub">${fmtBytes(disk.used)} / ${fmtBytes(disk.total)}</div>
        </div>
        <div class="hw-ring-wrap">${ringSvg(disk.percent, orDash(pct))}</div>
      </div>
      ${barHtml(disk.percent)}
      <div class="hw-kv">
        <div><div class="k">Used</div><div class="v" id="dash-disk-used">${fmtBytes(disk.used)}</div></div>
        <div><div class="k">Free</div><div class="v" id="dash-disk-free">${fmtBytes(disk.free)}</div></div>
        <div><div class="k">Total</div><div class="v" id="dash-disk-total">${fmtBytes(disk.total)}</div></div>
      </div>
      ${Array.isArray(disk.partitions) && disk.partitions.length
        ? `<div class="hw-foot" id="dash-disk-parts">${disk.partitions.slice(0, 3).map((p) => `${esc(p.mount || p.device || "?")} ${fmtPct(p.percent) || "—"}`).join(" · ")}</div>`
        : ""}`;
  }

  /* ---- RUNNING MODELS (ollama ps style table) ----
   * Data: snap.ollama.running — OllamaClient → /api/ps (the exact data
   * behind `ollama ps`). Processor split is the EXISTING honest derivation
   * from size vs size_vram (labelled derived — NOT a telemetry split).
   * Actions reuse the Running page handlers (window.Actions run-chat /
   * run-stop) — no duplicated functionality. */
  function splitInfo(m) {
    const size = m.size || 0;
    const vram = m.size_vram || 0;
    if (!size || !vram) return null;
    if (vram < size) return { kind: "split", gpuPct: Math.round((vram / size) * 100) };
    return { kind: "gpu" };
  }
  function processorCell(m) {
    const s = splitInfo(m);
    if (!s) return `<span class="text-faint" title="Ollama did not report size/size_vram — no split is invented">—</span>`;
    if (s.kind === "gpu") return `<span class="badge running-badge" title="derived from /api/ps size vs size_vram — not a telemetry split">100% GPU</span>`;
    return `<span class="badge cap" title="derived from /api/ps size vs size_vram — not a telemetry split: CPU ${fmtBytes(m.size - m.size_vram)}">${s.gpuPct}% GPU / ${100 - s.gpuPct}% CPU</span>`;
  }
  function runningTableHtml(rows) {
    if (!rows.length) {
      return `<div class="empty text-dim">${iconSvg("models")} No models loaded
        <div class="text-faint" style="margin-top:4px">Run one from <a href="#models">Models</a>.</div></div>`;
    }
    return `<div class="ps-table-wrap"><table class="ps-table">
      <thead><tr>
        <th>Model</th><th>ID</th><th class="num">Size</th><th>Processor</th>
        <th class="num">VRAM</th><th class="num">Context</th><th class="num">Duration</th><th>Status</th><th>Actions</th>
      </tr></thead>
      <tbody>
        ${rows.map((m) => {
          const id = m.digest ? String(m.digest).slice(0, 12) : null;
          const ctx = m.context_length != null ? m.context_length : (m.details && m.details.context_length);
          return `<tr>
            <td class="mono ps-model" title="${esc(m.name)}">${esc(m.name)}</td>
            <td class="mono text-dim">${id ? esc(id) : `<span class="text-faint">—</span>`}</td>
            <td class="num">${m.size != null ? fmtBytes(m.size) : "—"}</td>
            <td>${processorCell(m)}</td>
            <td class="num" style="color:var(--accent)">${m.size_vram != null ? fmtBytes(m.size_vram) : "—"}</td>
            <td class="num">${ctx != null ? fmtNum(ctx, 0) : `<span class="text-faint">—</span>`}</td>
            <td class="num text-dim" title="time until unload (expires_at)">${orDash(fmtDur(remainingSec(m.expires_at)))}</td>
            <td><span class="badge running-badge">● running</span></td>
            <td><div class="ps-actions">
              <button class="btn-sm" data-action="run-chat" data-model="${esc(m.name)}" title="Chat with this model">${iconSvg("ollama")} Chat</button>
              <button class="btn-sm btn-danger" data-action="run-stop" data-model="${esc(m.name)}" title="Unload from memory">⏹ Stop</button>
            </div></td>
          </tr>`;
        }).join("")}
      </tbody>
    </table>
    <div class="text-faint" style="margin:8px 2px 2px;font-size:11.5px">
      Processor split is derived from /api/ps size vs size_vram — not an exact telemetry split. Model VRAM ≠ GPU VRAM (see the GPU card).
    </div></div>`;
  }

  function drawRunning(snap) {
    const runEl = document.getElementById("dash-running");
    if (!runEl) return;
    const ollama = snap.ollama || {};
    if (ollama.online === false) {
      runEl.innerHTML = `<div class="empty text-dim">${iconSvg("ollama")} Ollama offline — running models unknown
        <div class="text-faint" style="margin-top:4px">Start Ollama or check the endpoint; the table updates automatically.</div></div>`;
      return;
    }
    if (!Array.isArray(ollama.running)) return; // partial snapshot: keep the last known table
    runEl.innerHTML = runningTableHtml(ollama.running);
  }

  /* ---- ELECTRICITY tile (compact summary) ----
   * Reads the EXISTING /api/electricity/gpu-energy payload (same endpoint
   * the Electricity page uses) — no new endpoint, no own sampler, no new
   * polling timer. Fetched once per render; a soft refresh piggybacks on
   * the existing websocket cadence with a 60 s throttle (stale-less, and
   * strictly LESS chatty than the page's own 5 s poll). GPU energy is
   * GPU-only and is never presented as total server energy. */
  let elecSummaryFetchedAt = 0;

  function elecTileHtml(e) {
    const open = 'open Electricity →';
    if (e && e.error) {
      return { value: 'unavailable', sub: `electricity API error · ${open}` };
    }
    const t = (e && e.tariff) || {};
    const today = (e && e.today) || {};
    const hasEnergy = today.available && today.kwh != null;
    if (!hasEnergy) {
      // sampler stopped / NVML absent / no aggregated points — honest empty
      return { value: 'data unavailable', sub: `no GPU energy telemetry yet · ${open}` };
    }
    if (!t.tariff_is_configured || t.tariff == null) {
      return { value: `${today.kwh.toFixed(4)} kWh`, sub: `tariff not configured · ${open}` };
    }
    const curCost = t.current_cost_per_hour;
    return {
      value: `${today.kwh.toFixed(4)} kWh · ${today.cost != null ? today.cost.toFixed(2) : "—"} ${esc(t.currency || "lei")}`,
      sub: `current cost ${curCost != null ? curCost.toFixed(3) + " " + esc(t.currency || "lei") + " / hour" : "unavailable"} · ${open}`,
    };
  }

  function renderElecTile(e) {
    const v = document.getElementById("dash-electricity-value");
    const s = document.getElementById("dash-electricity-sub");
    if (!v) return;
    const parts = elecTileHtml(e);
    v.textContent = parts.value;
    if (s) s.textContent = parts.sub;
  }

  async function loadElectricitySummary(force = false) {
    if (App.state.currentPage !== "dashboard") return;
    // WS-cadence soft refresh with a throttle — never its own timer
    const nowMs = Date.now();
    if (!force && nowMs - elecSummaryFetchedAt < 60000) return;
    elecSummaryFetchedAt = nowMs;
    let data;
    try {
      data = await API.get("/api/electricity/gpu-energy");
    } catch (err) {
      renderElecTile({ error: String((err && err.message) || err) });
      return;
    }
    renderElecTile(data);
  }

  /* Exposed for the stale-state test seam (module-internal time is not
   * directly manipulable from the harness). Not used by the UI flow. */
  function markProcStaleIfDue(nowMs) {
    const staleEl = document.getElementById("dash-proc-state");
    if (Date.now() - lastProcOk > 7000 && staleEl && lastProcOk) {
      staleEl.textContent = "· data stale (API unreachable)";
    }
    return staleEl ? staleEl.textContent : "";
  }
  function touchProcOk() { lastProcOk = Date.now(); }

  function startCharts() {
    const chartGrid = document.getElementById("dash-charts");
    if (!chartGrid) return;
    chartGrid.innerHTML = `
      <div class="card" style="margin:0"><div class="chart-box" style="height:130px"><canvas id="dash-chart-gpu"></canvas></div></div>
      <div class="card" style="margin:0"><div class="chart-box" style="height:130px"><canvas id="dash-chart-vram"></canvas></div></div>
      <div class="card" style="margin:0"><div class="chart-box" style="height:130px"><canvas id="dash-chart-temp"></canvas></div></div>
      <div class="card" style="margin:0"><div class="chart-box" style="height:130px"><canvas id="dash-chart-power"></canvas></div></div>
      <div class="card" style="margin:0"><div class="chart-box" style="height:130px"><canvas id="dash-chart-cpu"></canvas></div></div>
      <div class="card" style="margin:0"><div class="chart-box" style="height:130px"><canvas id="dash-chart-ram"></canvas></div></div>
      <div class="card" style="margin:0"><div class="chart-box" style="height:130px"><canvas id="dash-chart-net"></canvas></div></div>
      <div class="card" style="margin:0"><div class="chart-box" style="height:130px"><canvas id="dash-chart-disk"></canvas></div></div>
      <div class="card" style="margin:0"><div class="chart-box" style="height:130px"><canvas id="dash-chart-fan-target"></canvas></div></div>
      <div class="card" style="margin:0"><div class="chart-box" style="height:130px"><canvas id="dash-chart-fan-pwm"></canvas></div></div>`;
    charts = {
      gpu: seriesChart("dash-chart-gpu", "GPU util %", (p) => p.data, "#4f8cff"),
      vram: seriesChart("dash-chart-vram", "VRAM used GB", (p) => p.data, "#9b7bff"),
      temp: seriesChart("dash-chart-temp", "Temp °C", (p) => p.data, "#e5534b"),
      power: seriesChart("dash-chart-power", "Power W", (p) => p.data, "#e5b23c"),
      cpu: seriesChart("dash-chart-cpu", "CPU %", (p) => p.data, "#35c48a"),
      ram: seriesChart("dash-chart-ram", "RAM used GB", (p) => p.data, "#4f8cff"),
      net: seriesChart("dash-chart-net", "Network MB/s", (p) => p.data, "#7aa7ff"),
      disk: seriesChart("dash-chart-disk", "Disk used %", (p) => p.data, "#9b7bff"),
      fanTarget: seriesChart("dash-chart-fan-target", "Fan Target %", (p) => p.data, "#e5b23c"),
      fanPwm: seriesChart("dash-chart-fan-pwm", "Fan PWM %", (p) => p.data, "#35c48a"),
    };
  }

  /* ---- PROCESSOR: existing 3s polling, page-scoped, race-guarded ---- */
  function fmtLoad(load) {
    if (!load || !load.length) return null;
    return load.map((x) => (x == null ? "—" : Number(x).toFixed(1))).join(" / ");
  }

  function renderProcessor(data) {
    const body = document.getElementById("proc-body");
    const hostEl = document.getElementById("proc-host");
    if (!body) return;
    if (hostEl) hostEl.textContent = data && data.host ? String(data.host) : "";
    const stateEl = document.getElementById("dash-proc-state");
    if (!data || data.available !== true) {
      body.classList.add("proc-unavailable");
      body.innerHTML = `Processor information unavailable` +
        (data && data.reason ? ` <span class="proc-host">— ${esc(String(data.reason))}</span>` : "");
      if (stateEl) stateEl.textContent = "· unavailable";
      return;
    }
    body.classList.remove("proc-unavailable");
    if (stateEl) stateEl.textContent = "";
    const cpu = data.cpu || {};
    const gpus = data.gpus || [];
    const ollama = data.ollama || {};
    const models = ollama.running_models || [];
    const load = fmtLoad(cpu.load || (cpu.percent != null ? [] : null));
    const cores = cpu.cores_logical ?? cpu.threads ?? cpu.cores ?? null;
    const util = cpu.utilization ?? cpu.percent ?? null;
    let html = `<div class="proc-grid">`;
    html += `
      <div class="proc-card">
        <div class="proc-title">CPU <span class="val">${util != null ? Number(util).toFixed(0) + "%" : "—"}</span></div>
        <div class="gpu-bar proc-cpu-bar"><div class="bar"><div style="width:${util != null ? Math.max(0, Math.min(100, util)).toFixed(1) : 0}%;background:var(--accent)"></div></div></div>
        <div class="proc-sub">Load ${load || "—"}${cores ? ` / ${cores} cores` : ""}</div>
        ${cpu.cores_physical != null && cpu.cores_logical != null ? `<div class="proc-row"><span>Physical / logical</span><b>${cpu.cores_physical} / ${cpu.cores_logical}</b></div>` : ""}
      </div>`;
    if (!data.gpu_available) {
      html += `
      <div class="proc-card">
        <div class="proc-title">GPU</div>
        <div class="proc-unavailable">GPU information unavailable</div>
        ${data.gpu_reason ? `<div class="proc-host">${esc(String(data.gpu_reason))}</div>` : ""}
      </div>`;
    }
    for (const pg of gpus) {
      const used = pg.memory_used, total = pg.memory_total;
      const pct = used != null && total ? (used / total) * 100 : null;
      const memPct = pg.memory_utilization != null ? pg.memory_utilization : pct;
      html += `
      <div class="proc-card">
        <div class="proc-title">${esc(pg.name || "GPU")} <span class="proc-gpu-idx">#${pg.index}</span></div>
        <div class="proc-title" style="margin-bottom:2px"><span class="val">${pg.utilization != null ? Number(pg.utilization).toFixed(0) + "%" : "—"}</span></div>
        <div class="gpu-bar" style="margin-top:6px"><div class="bar"><div style="width:${pg.utilization != null ? Math.max(0, Math.min(100, pg.utilization)).toFixed(1) : 0}%;background:var(--accent)"></div></div></div>
        <div class="proc-sub">VRAM ${used != null ? fmtBytes(used) : "—"} / ${total != null ? fmtBytes(total) : "—"}${memPct != null ? ` (${Number(memPct).toFixed(0)}%)` : ""}</div>
        <div class="gpu-bar" style="margin-top:4px"><div class="bar"><div style="width:${memPct != null ? Math.max(0, Math.min(100, memPct)).toFixed(1) : 0}%;background:var(--accent-2, var(--accent))"></div></div></div>
        <div class="proc-row"><span>Temperature</span><b>${pg.temperature != null ? Number(pg.temperature).toFixed(0) + "°C" : "NOT AVAILABLE"}</b></div>
      </div>`;
    }
    const modelsHtml = models.length
      ? models.map((m) => `
          <div class="proc-row"><span>${esc(m.name || "unknown")}</span><b>${m.size_vram != null ? fmtBytes(m.size_vram) : "—"}</b></div>`).join("")
      : `<div class="proc-unavailable">${ollama.online ? "No models loaded" : "Ollama information unavailable"}</div>`;
    html += `
      <div class="proc-card">
        <div class="proc-title">OLLAMA <span class="val">${models.length || ""}</span></div>
        <div class="proc-sub">${ollama.cli_fallback_used ? "via ollama ps (CLI)" : "running models"}</div>
        ${modelsHtml}
        ${ollama.online && ollama.vram_used ? `<div class="proc-row"><span>Total VRAM in use</span><b>${fmtBytes(ollama.vram_used)}</b></div>` : ""}
      </div>`;
    html += `</div>`;
    body.innerHTML = html;
  }

  async function refreshProcessor() {
    if (App.state.currentPage !== "dashboard") return;
    const seq = ++procSeq;
    const staleEl = document.getElementById("dash-proc-state");
    try {
      const data = await API.get("/api/system/processor");
      if (seq !== procSeq) return; // a newer poll/render answered first
      renderProcessor(data);
      lastProcOk = Date.now();
      if (staleEl) staleEl.textContent = "";
    } catch (e) {
      if (seq !== procSeq) return;
      // network/auth failure: keep last good data, mark stale after 2 misses
      if (lastProcOk && Date.now() - lastProcOk > 7000 && staleEl) {
        staleEl.textContent = "· data stale (API unreachable)";
      }
    }
  }

  function startProcessorPolling() {
    refreshProcessor();
    if (procTimer) clearInterval(procTimer);
    procTimer = setInterval(refreshProcessor, 3000);
  }

  function stopProcessorPolling() {
    if (procTimer) { clearInterval(procTimer); procTimer = null; }
  }

  window.addEventListener("beforeunload", stopProcessorPolling);

  async function loadHistory(minutes) {
    try {
      const [gpuH, sysH] = await Promise.all([
        API.get(`/api/history/gpu?minutes=${minutes}`),
        API.get(`/api/history/system?minutes=${minutes}`),
      ]);
      if (!charts) return;
      const gpuPick = (field) => (p) => {
        const g = (p.data && p.data.gpus && p.data.gpus[0]);
        return g && g[field] != null ? g[field] : null;
      };
      charts.gpu.load(gpuH.points || [], gpuPick("utilization"));
      charts.vram.load(gpuH.points || [], (p) => {
        const g = (p.data && p.data.gpus && p.data.gpus[0]);
        return g && g.vram_used != null ? g.vram_used / (1024 ** 3) : null;
      });
      charts.temp.load(gpuH.points || [], gpuPick("temperature"));
      charts.power.load(gpuH.points || [], gpuPick("power_draw"));
      charts.cpu.load(sysH.points || [], (p) => p.data && p.data.cpu != null ? p.data.cpu : null);
      charts.ram.load(sysH.points || [], (p) => p.data && p.data.ram_used != null ? p.data.ram_used / (1024 ** 3) : null);
      charts.net.load(sysH.points || [], (p) => {
        const v = p.data && p.data.rx_rate != null ? (p.data.rx_rate + (p.data.tx_rate || 0)) / (1024 ** 2) : null;
        return v;
      });
      charts.disk.load(sysH.points || [], (p) => p.data && p.data.disk_used != null && p.data.disk_total ? (p.data.disk_used / p.data.disk_total) * 100 : null);
      charts.fanTarget.load(gpuH.points || [], gpuPick("fan_target"));
      charts.fanPwm.load(gpuH.points || [], gpuPick("fan_pwm"));
    } catch (e) {
      // history is auxiliary — a failure must not blank the dashboard
      const grid = document.getElementById("dash-charts");
      if (grid) grid.innerHTML = `<div class="empty text-dim" style="grid-column:1/-1">History unavailable — ${esc(e.message || "load failed")}</div>`;
    }
  }

  /* live updates from the existing websocket — single snapshot, no polling */
  function onSnapshot(snap) {
    if (App.state.currentPage !== "dashboard") return;
    drawSnapshot(snap, "");
    // electricity tile: throttled soft refresh riding the existing WS cadence
    loadElectricitySummary();
    // live chart pushes (same shared snapshot as the cards above)
    if (!charts) return;
    try {
      const gpu = snap.gpu || {};
      const g = (gpu.gpus || [])[0];
      const ts = snap.ts || Date.now() / 1000;
      const p = (v) => (v != null ? v : null);
      if (g) {
        if (charts.power) charts.power.push(ts, p(g.power_draw));
        if (charts.gpu) charts.gpu.push(ts, p(g.utilization));
        if (charts.vram && g.vram_used != null) charts.vram.push(ts, g.vram_used / (1024 ** 3));
        if (charts.temp) charts.temp.push(ts, p(g.temperature));
        if (charts.fanTarget && g.fan_target != null) charts.fanTarget.push(ts, g.fan_target);
        if (charts.fanPwm && g.fan_pwm != null) charts.fanPwm.push(ts, g.fan_pwm);
      }
      const cpuPct = snap.cpu && snap.cpu.percent != null ? snap.cpu.percent : null;
      if (charts.cpu) charts.cpu.push(ts, cpuPct);
      if (charts.ram && snap.ram && snap.ram.used != null) charts.ram.push(ts, snap.ram.used / (1024 ** 3));
      if (snap.disk && snap.disk.total) charts.disk.push(ts, (snap.disk.used / snap.disk.total) * 100);
    } catch (e) { console.error("chart live update failed", e); }
  }

  window.Pages = window.Pages || {};
  window.Pages.dashboard = {
    render, onSnapshot,
    drawSnapshot, drawCpu, drawRam, drawGpu, drawStorage, drawJobsTile,
    drawRunning, runningTableHtml, processorCell, splitInfo,
    fmtPct, fmtWatts, fmtTempC, fmtGb, fmtMhz, fmtDur, ringSvg,
    renderProcessor, startProcessorPolling, stopProcessorPolling,
    markProcStaleIfDue, touchProcOk, refreshProcessor,
    loadElectricitySummary, renderElecTile, iconSvg,
  };
})();
