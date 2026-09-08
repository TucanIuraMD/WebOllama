/* Dashboard v2 — single daily-driver overview after Models v2 / Running v2 /
 * PROCESSOR / GPU sync / Chat v2-v3.
 *
 * Data sources (unchanged architecture — ONE realtime snapshot is the truth):
 *   - WS snapshot (snap): gpu + cpu/ram + ollama (online, counts, running)
 *     — same object the topbar and PROCESSOR (via /api/system/processor)
 *     consume, so GPU/VRAM numbers cannot diverge between blocks;
 *   - /api/system/processor — polled every 3s ONLY while the Dashboard is
 *     the active page (existing PROCESSOR contract, timer killed on page
 *     switch and beforeunload);
 *   - /api/history/{gpu,system} — history charts (no live polling).
 *
 * No new backend endpoints. No duplicate GPU telemetry: the GPU block, the
 * GPU tile and the PROCESSOR block all render from the shared snapshot
 * payload, and model VRAM (ollama running size_vram) is always shown
 * separately from GPU VRAM.
 *
 * States per block: loading / available / empty / offline / stale.
 * An API error NEVER renders as "no data".
 */
(function () {
  let charts = null;
  let procTimer = null;
  let procSeq = 0;      // race guard: only the latest processor response wins
  let lastProcOk = 0;   // timestamp of the last successful processor poll

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
  }

  function buildPage(snap, loadError) {
    const offline = !snap || !(snap.ollama && snap.ollama.online);
    return `
      ${loadError ? `<div class="alert error" id="dash-alert">⚠ Dashboard data unavailable — ${esc(loadError)}. <a href="#dashboard">Retry</a> or check the backend.</div>` : ""}

      <div class="dash-nav">
        <a class="btn-sm" href="#models">📦 Models</a>
        <a class="btn-sm" href="#running">▶ Running</a>
        <a class="btn-sm" href="#jobs">⚙ Jobs</a>
        <a class="btn-sm" href="#chat">💬 Chat</a>
        <a class="btn-sm" href="#agents">🤖 Agents</a>
        <a class="btn-sm" href="#gpu">🎮 GPU</a>
      </div>

      <div class="grid grid-4">
        <div class="metric-tile ${offline ? "bad" : "good"}" id="dash-ollama-tile">
          <div class="metric-label">Ollama</div>
          <div class="metric-value" style="font-size:16px" id="dash-ollama-state">${offline && snap ? "● OLLAMA OFFLINE" : snap ? "● ONLINE" : "⏳ Loading…"}</div>
          <div class="metric-sub" id="dash-ollama-sub">${snap && snap.ollama ? `v${esc(snap.ollama.version || "—")} · ${esc(snap.ollama.endpoint || "")}` : "waiting for data…"}</div>
        </div>
        <a class="metric-tile dash-link" href="#models" id="dash-models-tile">
          <div class="metric-label">Models</div>
          <div class="metric-value" id="dash-models-count">—</div>
          <div class="metric-sub" id="dash-models-sub">installed · open Models →</div>
        </a>
        <a class="metric-tile dash-link" href="#running" id="dash-running-tile">
          <div class="metric-label">Running</div>
          <div class="metric-value" id="dash-running-count">—</div>
          <div class="metric-sub">loaded · open Running →</div>
        </a>
        <a class="metric-tile dash-link" href="#electricity" id="dash-electricity-tile">
          <div class="metric-label">Electricity</div>
          <div class="metric-value" id="dash-electricity-value">—</div>
          <div class="metric-sub">power & cost · open Electricity →</div>
        </a>
        <div class="metric-tile" id="dash-cpu-tile">
          <div class="metric-label">CPU</div>
          <div class="metric-value" id="dash-cpu-value">—</div>
          <div class="metric-sub" id="dash-cpu-sub">waiting for data…</div>
        </div>
      </div>

      <div class="card" style="margin-top:12px">
        <div class="card-title">PROCESSOR <span class="right"><span class="proc-host" id="proc-host"></span><span class="text-faint" id="dash-proc-state"></span></span></div>
        <div id="proc-body" class="proc-unavailable">⏳ Loading processor data…</div>
      </div>

      <div class="grid grid-2">
        <div class="card">
          <div class="card-title">GPU <span id="dash-gpu-title"></span></div>
          <div id="dash-gpu-body"><div class="empty"><div class="big">⏳</div>Loading GPU data…</div></div>
        </div>

        <div class="card">
          <div class="card-title">Running Models <span class="right"><a class="btn-sm" href="#running">open Running →</a></span></div>
          <div id="dash-running"><div class="empty text-dim">⏳ Loading…</div></div>
        </div>
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
        oSub.textContent = `v${ollama.version || "—"} · ${ollama.endpoint || ""}`;
      }
      // Models / Running tiles (counts from the same snapshot)
      const mc = document.getElementById("dash-models-count");
      if (mc) mc.textContent = ollama.models_count != null ? String(ollama.models_count) : "—";
      const rc = document.getElementById("dash-running-count");
      if (rc) rc.textContent = ollama.running_count != null ? String(ollama.running_count) : (ollama.online ? "0" : "—");
      drawRunning(snap);
    }
    if (snap.gpu) drawGpu(snap.gpu);
    if (snap.cpu) {
      const cpu = snap.cpu;
      const cv = document.getElementById("dash-cpu-value");
      if (cv) cv.textContent = cpu.percent != null ? Number(cpu.percent).toFixed(0) + "%" : "—";
      const cs = document.getElementById("dash-cpu-sub");
      if (cs) cs.textContent = cpu.load_1 != null ? `load ${Number(cpu.load_1).toFixed(2)}` : "—";
    }
  }

  /* ONE GPU source: the shared snapshot's gpu object. The PROCESSOR block
   * (polled) serves the same numbers from the same snapshot — see
   * test_processor_matches_dashboard_gpu_block on the backend. */
  function drawGpu(gpu) {
    const body = document.getElementById("dash-gpu-body");
    const title = document.getElementById("dash-gpu-title");
    if (!body) return;
    const gpus = gpu.gpus || [];
    const g = gpus[0] || null;

    if (!gpu.available) {
      // explicit unavailable — GPU-less hosts are NORMAL, not an error
      title.textContent = "";
      body.innerHTML = `<div class="empty"><div class="big">🎮</div>${esc(gpu.reason || "No NVIDIA GPU detected on this host")}
        <div class="text-faint" style="margin-top:6px">CPU-only Ollama works fine — GPU blocks fill in automatically when an NVIDIA GPU is present.</div></div>`;
      return;
    }
    if (!g) {
      body.innerHTML = `<div class="empty"><div class="big">🎮</div>GPU available, no devices reported<div class="text-faint">Check the GPU page for details.</div></div>`;
      return;
    }

    title.textContent = `· ${g.name || "GPU"}`;
    const vramPct = g.vram_total ? ((g.vram_used / g.vram_total) * 100).toFixed(0) : null;
    const modelVram = gpu.ollama_vram && gpu.ollama_vram.total_vram;
    body.innerHTML = `
      <div style="display:grid;gap:12px">
        <div>
          <div class="metric-label" style="margin-bottom:4px">GPU Utilization</div>
          <div id="dash-gpu-util"></div>
        </div>
        <div>
          <div class="metric-label" style="margin-bottom:4px">GPU VRAM ${fmtBytes(g.vram_used)} / ${fmtBytes(g.vram_total)}${vramPct ? " (" + vramPct + "%)" : ""}</div>
          <div id="dash-vram-bar"></div>
        </div>
        <div class="grid grid-3">
          <div><div class="metric-label">Temp</div><div class="metric-value" style="font-size:18px" id="dash-temp">${g.temperature != null ? Number(g.temperature).toFixed(0) + "°C" : "—"}</div></div>
          <div><div class="metric-label">Power</div><div class="metric-value" style="font-size:18px" id="dash-power">${g.power_draw != null ? Number(g.power_draw).toFixed(0) + " W" : "—"}</div></div>
          <div><div class="metric-label">Fan Control</div><div class="metric-value" style="font-size:15px;line-height:1.35" id="dash-fan">
            ${g.fan_available
              ? `Target: ${g.fan_target != null ? Number(g.fan_target).toFixed(0) : "—"}%<br/>PWM: ${g.fan_pwm != null ? Number(g.fan_pwm).toFixed(0) : "—"}%`
              : `Target: —<br/>PWM: —<br/><span style="font-size:11px;color:var(--text-faint)">Status: unavailable</span>`}
          </div></div>
        </div>
        <div class="grid grid-3">
          <div><div class="metric-label">Clocks</div><div class="metric-sub mono" id="dash-clocks">${g.clocks != null ? g.clocks + " MHz" : "—"}</div></div>
          <div><div class="metric-label">Mem Clock</div><div class="metric-sub mono" id="dash-memclock">${g.mem_clock != null ? g.mem_clock + " MHz" : "—"}</div></div>
          <div><div class="metric-label">Model VRAM (Ollama)</div><div class="metric-sub mono" id="dash-modelvram">${modelVram ? fmtBytes(modelVram) : "—"}</div></div>
        </div>
      </div>`;
    renderUtilBar(document.getElementById("dash-gpu-util"), g.utilization ?? null);
    const vramBar = document.getElementById("dash-vram-bar");
    if (vramBar && g.vram_total) {
      const p = (g.vram_used / g.vram_total) * 100;
      vramBar.innerHTML = `<div class="gpu-bar"><div class="bar"><div style="width:${p.toFixed(1)}%;background:var(--accent)"></div></div><span class="pct">${vramPct || 0}%</span></div>`;
    }
  }

  /* Compact running block — full workflow lives on the Running page. */
  function drawRunning(snap) {
    const runEl = document.getElementById("dash-running");
    if (!runEl) return;
    const ollama = snap.ollama || {};
    if (!ollama.online) {
      runEl.innerHTML = `<div class="empty text-dim">🔌 Ollama offline — running models unknown</div>`;
      return;
    }
    const running = ollama.running || [];
    if (!running.length) {
      runEl.innerHTML = `<div class="empty text-dim">🌙 No models loaded
        <div class="text-faint" style="margin-top:4px">Run one from <a href="#models">Models</a>.</div></div>`;
      return;
    }
    runEl.innerHTML = running.map((m) => {
      const size = m.size || 0, vram = m.size_vram || 0;
      // split badge only when the data is really there (no heuristics)
      const split = (size && vram && vram < size)
        ? ` · ${Math.round((vram / size) * 100)}% GPU`
        : (size && vram && vram >= size ? " · 100% GPU" : "");
      return `
        <div style="display:flex;justify-content:space-between;align-items:center;padding:7px 0;border-bottom:1px solid var(--border)">
          <div>
            <div class="mono" style="word-break:break-all">${esc(m.name)}</div>
            <div class="text-faint" style="font-size:11px">${fmtBytes(vram)} model VRAM${split}</div>
          </div>
          <span class="badge loaded">loaded</span>
        </div>`;
    }).join("");
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
    // live GPU bars + tiles (same shared snapshot as the GPU block above)
    const gpu = snap.gpu || {};
    const g = (gpu.gpus || [])[0];
    if (g) {
      const utilEl = document.getElementById("dash-gpu-util");
      if (utilEl) renderUtilBar(utilEl, g.utilization ?? null);
      const vramBar = document.getElementById("dash-vram-bar");
      if (vramBar && g.vram_total) {
        const p = (g.vram_used / g.vram_total) * 100;
        vramBar.innerHTML = `<div class="gpu-bar"><div class="bar"><div style="width:${p.toFixed(1)}%;background:var(--accent)"></div></div><span class="pct">${p.toFixed(0)}%</span></div>`;
      }
      const powerEl = document.getElementById("dash-power");
      if (powerEl) powerEl.textContent = `${g.power_draw != null ? Number(g.power_draw).toFixed(0) + " W" : "—"}`;
      const tempEl = document.getElementById("dash-temp");
      if (tempEl) tempEl.textContent = `${g.temperature != null ? Number(g.temperature).toFixed(0) + "°C" : "—"}`;
      const fanEl = document.getElementById("dash-fan");
      if (fanEl) {
        fanEl.innerHTML = g.fan_available
          ? `Target: ${g.fan_target != null ? Number(g.fan_target).toFixed(0) : "—"}%<br/>PWM: ${g.fan_pwm != null ? Number(g.fan_pwm).toFixed(0) : "—"}%`
          : `Target: —<br/>PWM: —<br/><span style="font-size:11px;color:var(--text-faint)">Status: unavailable</span>`;
      }
      const clockEl = document.getElementById("dash-clocks");
      if (clockEl) clockEl.textContent = `${g.clocks != null ? g.clocks + " MHz" : "—"}`;
      const memEl = document.getElementById("dash-memclock");
      if (memEl) memEl.textContent = `${g.mem_clock != null ? g.mem_clock + " MHz" : "—"}`;
      if (charts) {
        try {
          const ts = snap.ts || Date.now() / 1000;
          const p = (v) => (v != null ? v : null);
          if (charts.power) charts.power.push(ts, p(g.power_draw));
          if (charts.gpu) charts.gpu.push(ts, p(g.utilization));
          if (charts.vram && g.vram_used != null) charts.vram.push(ts, g.vram_used / (1024 ** 3));
          if (charts.temp) charts.temp.push(ts, p(g.temperature));
          if (charts.fanTarget && g.fan_target != null) charts.fanTarget.push(ts, g.fan_target);
          if (charts.fanPwm && g.fan_pwm != null) charts.fanPwm.push(ts, g.fan_pwm);
        } catch (e) { console.error("chart live update failed", e); }
      }
    }
  }

  window.Pages = window.Pages || {};
  window.Pages.dashboard = { render, onSnapshot, drawSnapshot, drawGpu, drawRunning, renderProcessor, startProcessorPolling, stopProcessorPolling, markProcStaleIfDue, touchProcOk, refreshProcessor };
})();
