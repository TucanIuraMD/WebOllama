/* Dashboard page — live GPU/Ollama/CPU overview + history charts */
(function () {
  let charts = null;

  async function render(el) {
    Charts.destroyAll("dash");
    let snap;
    try {
      snap = App.state.snapshot || await API.get("/api/status");
    } catch (e) {
      snap = { ollama: { online: false }, gpu: { available: false } };
    }
    const ollama = snap.ollama || {};
    const gpu = snap.gpu || {};
    const running = ollama.running || [];
    const gpus = gpu.gpus || [];
    const g = gpus[0] || null;

    const vramPct = g && g.vram_total ? ((g.vram_used / g.vram_total) * 100).toFixed(0) : null;
    const utilPct = g ? (g.utilization ?? null) : null;

    el.innerHTML = `
      <div class="grid grid-4">
        <div class="metric-tile ${ollama.online ? "good" : "bad"}">
          <div class="metric-label">Ollama</div>
          <div class="metric-value" style="font-size:16px">${ollama.online ? "● ONLINE" : "● OLLAMA OFFLINE"}</div>
          <div class="metric-sub">v${ollama.version || "—"} · ${esc(ollama.endpoint || "")}</div>
        </div>
        <div class="metric-tile">
          <div class="metric-label">Models</div>
          <div class="metric-value">${ollama.models_count ?? "—"}</div>
          <div class="metric-sub">${(ollama.running_count ?? 0)} loaded</div>
        </div>
        <div class="metric-tile ${g ? "good" : ""}" id="dash-gpu-summary">
          <div class="metric-label">GPU${g ? " · " + esc(g.name) : ""}</div>
          <div class="metric-value" style="font-size:16px">${g ? esc(g.name || "GPU") : "No NVIDIA GPU"}</div>
          <div class="metric-sub" id="dash-gpu-sub">${g ? `${g.temperature ?? "—"}°C · ${g.power_draw ?? "—"} W` : esc(gpu.reason || "not available")}</div>
        </div>
        <div class="metric-tile">
          <div class="metric-label">CPU</div>
          <div class="metric-value">${snap.cpu ? (snap.cpu.percent ?? 0).toFixed(0) + "%" : "—"}</div>
          <div class="metric-sub">load ${snap.cpu && snap.cpu.load_1 ? snap.cpu.load_1.toFixed(2) : "—"}</div>
        </div>
      </div>

      <div class="card" style="margin-top:12px">
        <div class="card-title">PROCESSOR <span class="right"><span class="proc-host" id="proc-host"></span></span></div>
        <div id="proc-body" class="proc-unavailable">Loading…</div>
      </div>

      <div class="grid grid-2">
        <div class="card">
          <div class="card-title">GPU ${g ? "· " + esc(g.name) : ""}</div>
          ${g ? `
            <div style="display:grid;gap:12px">
              <div>
                <div class="metric-label" style="margin-bottom:4px">GPU Utilization</div>
                <div id="dash-gpu-util"></div>
              </div>
              <div>
                <div class="metric-label" style="margin-bottom:4px">VRAM ${fmtBytes(g.vram_used)} / ${fmtBytes(g.vram_total)}${vramPct ? " (" + vramPct + "%)" : ""}</div>
                <div id="dash-vram-bar"></div>
              </div>
              <div class="grid grid-3">
                <div><div class="metric-label">Temp</div><div class="metric-value" style="font-size:18px" id="dash-temp">${g.temperature ?? "—"}°C</div></div>
                <div><div class="metric-label">Power</div><div class="metric-value" style="font-size:18px" id="dash-power">${g.power_draw ?? "—"} W</div></div>
                <div><div class="metric-label">Fan Control</div><div class="metric-value" style="font-size:15px;line-height:1.35" id="dash-fan">
                  ${g.fan_available
                    ? `Target: ${g.fan_target ?? "—"}%<br/>PWM: ${g.fan_pwm ?? "—"}%`
                    : `Target: —<br/>PWM: —<br/><span style="font-size:11px;color:var(--text-faint)">Status: unavailable</span>`}
                </div></div>
              </div>
              <div class="grid grid-3">
                <div><div class="metric-label">Clocks</div><div class="metric-sub mono" id="dash-clocks">${g.clocks ?? "—"} MHz</div></div>
                <div><div class="metric-label">Mem Clock</div><div class="metric-sub mono" id="dash-memclock">${g.mem_clock ?? "—"} MHz</div></div>
                <div><div class="metric-label">VRAM / Model</div><div class="metric-sub mono">${fmtBytes(gpu.ollama_vram && gpu.ollama_vram.total_vram)}</div></div>
              </div>
            </div>
          ` : `
            <div class="empty">
              <div class="big">🎮</div>
              ${esc(gpu.reason || "No NVIDIA GPU detected on this host")}
              <div class="text-faint" style="margin-top:6px">With a Tesla V100 present, utilization / VRAM / temp / power / fan control appear here live.</div>
            </div>
          `}
        </div>

        <div class="card">
          <div class="card-title">Running Models</div>
          <div id="dash-running"></div>
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
      </div>
    `;

    // GPU bars
    if (g) {
      renderUtilBar(document.getElementById("dash-gpu-util"), utilPct);
      const vramBar = document.getElementById("dash-vram-bar");
      const vramP = g.vram_total ? (g.vram_used / g.vram_total) * 100 : 0;
      vramBar.innerHTML = `<div class="gpu-bar"><div class="bar"><div style="width:${vramP.toFixed(1)}%;background:var(--accent)"></div></div><span class="pct">${vramPct || 0}%</span></div>`;
    }

    // running models
    const runEl = document.getElementById("dash-running");
    if (!running.length) {
      runEl.innerHTML = '<div class="empty text-dim">No models loaded</div>';
    } else {
      runEl.innerHTML = running.map((m) => `
        <div style="display:flex;justify-content:space-between;align-items:center;padding:7px 0;border-bottom:1px solid var(--border)">
          <div>
            <div class="mono" style="word-break:break-all">${esc(m.name)}</div>
            <div class="text-faint" style="font-size:11px">${fmtBytes(m.size_vram)} VRAM</div>
          </div>
          <span class="badge loaded">loaded</span>
        </div>`).join("");
    }

    // charts
    const chartGrid = document.getElementById("dash-charts");
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
      <div class="card" style="margin:0"><div class="chart-box" style="height:130px"><canvas id="dash-chart-fan-pwm"></canvas></div></div>
    `;

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

    // range buttons
    document.querySelectorAll(".dash-range").forEach((btn) => {
      btn.onclick = () => {
        document.querySelectorAll(".dash-range").forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
        loadHistory(Number(btn.dataset.min));
        document.getElementById("dash-range-label").textContent =
          btn.dataset.min === "1" ? "1 minute" : btn.dataset.min === "5" ? "5 minutes" : btn.dataset.min === "15" ? "15 minutes" : "1 hour";
      };
    });

    loadHistory(60);
    startProcessorPolling();
  }

  // ------------------------------------------------------------------
  // PROCESSOR — CPU/GPU/Ollama of the machine running WebOllama (after
  // deployment that is the Ollama server itself). Data is collected
  // locally: GPUCollector + SystemCollector + Ollama /api/ps (ollama ps).
  // Polls /api/system/processor every 3s while the Dashboard is visible;
  // structured failure -> friendly text, never a traceback.
  // ------------------------------------------------------------------
  let procTimer = null;

  function fmtLoad(load) {
    if (!load || !load.length) return null;
    return load.map((x) => (x == null ? "—" : Number(x).toFixed(1))).join(" / ");
  }

  function renderProcessor(data) {
    const body = document.getElementById("proc-body");
    const hostEl = document.getElementById("proc-host");
    if (!body) return;
    if (hostEl) hostEl.textContent = data && data.host ? String(data.host) : "";
    if (!data || data.available !== true) {
      body.classList.add("proc-unavailable");
      body.innerHTML = `Processor information unavailable` +
        (data && data.reason ? ` <span class="proc-host">— ${esc(String(data.reason))}</span>` : "");
      return;
    }
    body.classList.remove("proc-unavailable");
    const cpu = data.cpu || {};
    const gpus = data.gpus || [];
    const ollama = data.ollama || {};
    const models = ollama.running_models || [];
    const load = fmtLoad(cpu.load);
    const cores = cpu.cores_logical ?? cpu.cores_physical ?? null;
    let html = `<div class="proc-grid">`;
    // CPU card
    html += `
      <div class="proc-card">
        <div class="proc-title">CPU <span class="val">${cpu.utilization != null ? Number(cpu.utilization).toFixed(0) + "%" : "—"}</span></div>
        <div class="gpu-bar proc-cpu-bar"><div class="bar"><div style="width:${cpu.utilization != null ? Math.max(0, Math.min(100, cpu.utilization)).toFixed(1) : 0}%;background:var(--accent)"></div></div></div>
        <div class="proc-sub">Load ${load || "—"}${cores ? ` / ${cores} cores` : ""}</div>
        ${cpu.cores_physical != null && cpu.cores_logical != null ? `<div class="proc-row"><span>Physical / logical</span><b>${cpu.cores_physical} / ${cpu.cores_logical}</b></div>` : ""}
      </div>`;
    // One card per GPU (or a friendly note when the host has none)
    if (!data.gpu_available) {
      html += `
      <div class="proc-card">
        <div class="proc-title">GPU</div>
        <div class="proc-unavailable">GPU information unavailable</div>
        ${data.gpu_reason ? `<div class="proc-host">${esc(String(data.gpu_reason))}</div>` : ""}
      </div>`;
    }
    for (const gpu of gpus) {
      const used = gpu.memory_used, total = gpu.memory_total;
      const pct = used != null && total ? (used / total) * 100 : null;
      const memPct = gpu.memory_utilization != null ? gpu.memory_utilization : pct;
      html += `
      <div class="proc-card">
        <div class="proc-title">${esc(gpu.name || "GPU")} <span class="proc-gpu-idx">#${gpu.index}</span></div>
        <div class="proc-title" style="margin-bottom:2px"><span class="val">${gpu.utilization != null ? Number(gpu.utilization).toFixed(0) + "%" : "—"}</span></div>
        <div class="gpu-bar" style="margin-top:6px"><div class="bar"><div style="width:${gpu.utilization != null ? Math.max(0, Math.min(100, gpu.utilization)).toFixed(1) : 0}%;background:var(--accent)"></div></div></div>
        <div class="proc-sub">VRAM ${used != null ? fmtBytes(used) : "—"} / ${total != null ? fmtBytes(total) : "—"}${memPct != null ? ` (${Number(memPct).toFixed(0)}%)` : ""}</div>
        <div class="gpu-bar" style="margin-top:4px"><div class="bar"><div style="width:${memPct != null ? Math.max(0, Math.min(100, memPct)).toFixed(1) : 0}%;background:var(--accent-2, var(--accent))"></div></div></div>
        <div class="proc-row"><span>Temperature</span><b>${gpu.temperature != null ? Number(gpu.temperature).toFixed(0) + "°C" : "NOT AVAILABLE"}</b></div>
      </div>`;
    }
    // Ollama card — running models (the data behind `ollama ps`)
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
    try {
      renderProcessor(await API.get("/api/system/processor"));
    } catch (e) {
      // network/auth failure: keep whatever is shown, degrade quietly
      const body = document.getElementById("proc-body");
      if (body && !body.innerHTML) {
        body.classList.add("proc-unavailable");
        body.textContent = "Processor information unavailable";
      }
    }
  }

  function startProcessorPolling() {
    refreshProcessor();
    if (procTimer) clearInterval(procTimer);
    procTimer = setInterval(refreshProcessor, 3000);
  }

  window.addEventListener("beforeunload", () => { if (procTimer) clearInterval(procTimer); });

  async function loadHistory(minutes) {
    try {
      const [gpuH, sysH] = await Promise.all([
        API.get(`/api/history/gpu?minutes=${minutes}`),
        API.get(`/api/history/system?minutes=${minutes}`),
      ]);
      if (!charts) return;
      charts.gpu.load(gpuH.points || [], (p) => {
        const g = (p.data && p.data.gpus && p.data.gpus[0]);
        return g && g.utilization != null ? g.utilization : null;
      });
      charts.vram.load(gpuH.points || [], (p) => {
        const g = (p.data && p.data.gpus && p.data.gpus[0]);
        return g && g.vram_used != null ? g.vram_used / (1024 ** 3) : null;
      });
      charts.temp.load(gpuH.points || [], (p) => {
        const g = (p.data && p.data.gpus && p.data.gpus[0]);
        return g && g.temperature != null ? g.temperature : null;
      });
      charts.power.load(gpuH.points || [], (p) => {
        const g = (p.data && p.data.gpus && p.data.gpus[0]);
        return g && g.power_draw != null ? g.power_draw : null;
      });
      charts.cpu.load(sysH.points || [], (p) => p.data && p.data.cpu != null ? p.data.cpu : null);
      charts.ram.load(sysH.points || [], (p) => p.data && p.data.ram_used != null ? p.data.ram_used / (1024 ** 3) : null);
      charts.net.load(sysH.points || [], (p) => {
        const v = p.data && p.data.rx_rate != null ? (p.data.rx_rate + (p.data.tx_rate || 0)) / (1024 ** 2) : null;
        return v;
      });
      charts.disk.load(sysH.points || [], (p) => p.data && p.data.disk_used != null && p.data.disk_total ? (p.data.disk_used / p.data.disk_total) * 100 : null);
      charts.fanTarget.load(gpuH.points || [], (p) => {
        const g = (p.data && p.data.gpus && p.data.gpus[0]);
        return g && g.fan_target != null ? g.fan_target : null;
      });
      charts.fanPwm.load(gpuH.points || [], (p) => {
        const g = (p.data && p.data.gpus && p.data.gpus[0]);
        return g && g.fan_pwm != null ? g.fan_pwm : null;
      });
    } catch (e) {
      console.error("history load failed", e);
    }
  }

  // live updates from websocket
  function onSnapshot(snap) {
    const gpu = snap.gpu || {};
    const g = (gpu.gpus || [])[0];
    const ts = snap.ts || Date.now() / 1000;
    if (g) {
      // utilization bar
      const utilEl = document.getElementById("dash-gpu-util");
      if (utilEl) renderUtilBar(utilEl, g.utilization ?? null);
      // VRAM bar
      const vramBar = document.getElementById("dash-vram-bar");
      if (vramBar && g.vram_total) {
        const p = (g.vram_used / g.vram_total) * 100;
        vramBar.innerHTML = `<div class="gpu-bar"><div class="bar"><div style="width:${p.toFixed(1)}%;background:var(--accent)"></div></div><span class="pct">${p.toFixed(0)}%</span></div>`;
      }
      // Power / Temp / Fan Control / Clocks / Mem Clock tiles
      const powerEl = document.getElementById("dash-power");
      if (powerEl) powerEl.textContent = `${g.power_draw ?? "—"} W`;
      const tempEl = document.getElementById("dash-temp");
      if (tempEl) tempEl.textContent = `${g.temperature ?? "—"}°C`;
      const fanEl = document.getElementById("dash-fan");
      if (fanEl) {
        fanEl.innerHTML = g.fan_available
          ? `Target: ${g.fan_target ?? "—"}%<br/>PWM: ${g.fan_pwm ?? "—"}%`
          : `Target: —<br/>PWM: —<br/><span style="font-size:11px;color:var(--text-faint)">Status: unavailable</span>`;
      }
      const clockEl = document.getElementById("dash-clocks");
      if (clockEl) clockEl.textContent = `${g.clocks ?? "—"} MHz`;
      const memEl = document.getElementById("dash-memclock");
      if (memEl) memEl.textContent = `${g.mem_clock ?? "—"} MHz`;
      const gpuSub = document.getElementById("dash-gpu-sub");
      if (gpuSub) gpuSub.textContent = `${g.temperature ?? "—"}°C · ${g.power_draw ?? "—"} W`;
    }
    // append live points to charts so they keep up with current data
    // (guarded: a chart failure must never break the live update)
    if (charts && g) {
      try {
        const p = (v) => (v != null ? v : null);
        if (charts.power) charts.power.push(ts, p(g.power_draw));
        if (charts.gpu) charts.gpu.push(ts, p(g.utilization));
        if (charts.vram && g.vram_used != null) charts.vram.push(ts, g.vram_used / (1024 ** 3));
        if (charts.temp) charts.temp.push(ts, p(g.temperature));
        if (charts.fanTarget && g.fan_target != null) charts.fanTarget.push(ts, g.fan_target);
        if (charts.fanPwm && g.fan_pwm != null) charts.fanPwm.push(ts, g.fan_pwm);
      } catch (e) { console.error("chart live update failed", e); }
    }
    // live running list
    const runEl = document.getElementById("dash-running");
    if (runEl && snap.ollama && App.state.currentPage === "dashboard") {
      const running = snap.ollama.running || [];
      if (!running.length) {
        runEl.innerHTML = '<div class="empty text-dim">No models loaded</div>';
      } else {
        runEl.innerHTML = running.map((m) => `
          <div style="display:flex;justify-content:space-between;align-items:center;padding:7px 0;border-bottom:1px solid var(--border)">
            <div>
              <div class="mono" style="word-break:break-all">${esc(m.name)}</div>
              <div class="text-faint" style="font-size:11px">${fmtBytes(m.size_vram)} VRAM</div>
            </div>
            <span class="badge loaded">loaded</span>
          </div>`).join("");
      }
    }
  }

  window.Pages = window.Pages || {};
  window.Pages.dashboard = { render, onSnapshot };
})();
