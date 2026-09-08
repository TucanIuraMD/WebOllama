/* Electricity page — host power telemetry, GPU energy, cost estimate.
 *
 * Data honesty contract (mirrors the backend):
 *  - Host `watts` is shown only when a HOST-level source (RAPL / hwmon)
 *    exists; otherwise "TOTAL SERVER — data unavailable" with the reason.
 *  - GPU power/energy is GPU-ONLY telemetry, always labeled as such and
 *    never presented as total server power.
 *  - GPU current W comes from the realtime GPU collector; average W / Wh
 *    come from aggregated gpu_energy rows (10 s points, gaps excluded).
 *  - kWh/Wh are CALCULATED; cost is CALCULATED (energy × tariff) and shown
 *    only when the admin configured a tariff — never an invented default.
 * Polls /api/electricity every 5s with a page-scoped timer (same pattern
 * as the Dashboard: single setInterval, cleared on re-render). GPU energy
 * windows refresh every 30s via the same single timer tick.
 */
(function () {
  let elecTimer = null;
  let tickCount = 0;

  async function render(el) {
    if (elecTimer) { clearInterval(elecTimer); elecTimer = null; }
    el.innerHTML = '<div class="empty text-dim">loading…</div>';
    let data;
    try {
      data = await API.get("/api/electricity");
    } catch (e) {
      el.innerHTML = `<div class="card"><div class="card-title">Electricity</div>
        <div class="empty"><div class="big">⚠</div>API error: ${esc(e.message)}</div></div>`;
      return;
    }
    el.innerHTML = build(data);
    wireActions();
    await refreshGpuEnergy();
    elecTimer = setInterval(tick, 5000);
  }

  function destroy() {
    if (elecTimer) { clearInterval(elecTimer); elecTimer = null; }
  }

  async function tick() {
    if (App.state.currentPage !== "electricity") { destroy(); return; }
    tickCount++;
    const every6 = tickCount % 6 === 0; // ~30 s
    try {
      const data = await API.get("/api/electricity");
      const sec = document.getElementById("elec-live");
      if (sec) sec.outerHTML = liveSection(data);
    } catch { /* transient — keep last good section */ }
    if (every6) await refreshGpuEnergy();
  }

  // ---- host section -------------------------------------------------------
  function liveSection(data) {
    if (!data.available) {
      return `<div class="card" id="elec-live">
        <div class="card-title">Total server power</div>
        <div class="empty">
          <div class="big">⚡</div>
          <b>TOTAL SERVER — data unavailable</b>
          <div class="text-faint" style="margin-top:8px;max-width:640px">${esc(data.reason || "no host-level power telemetry source on this machine")}</div>
        </div>
      </div>`;
    }
    const c = data.components || {};
    return `<div class="card" id="elec-live">
      <div class="card-title">Total server power <span class="right status-pill status-on">measured · ${esc(data.source || "")}</span></div>
      <div class="grid grid-4">
        ${tile("Total server power", data.watts != null ? data.watts.toFixed(1) + " W" : "—")}
        ${tile("CPU (RAPL)", c.cpu && c.cpu.watts != null ? c.cpu.watts.toFixed(1) + " W" : "—")}
        ${tile("Platform (hwmon)", c.platform && c.platform.watts != null ? c.platform.watts.toFixed(1) + " W" : "—")}
      </div>
      <div class="text-faint" style="font-size:12px;margin-top:10px">Host-level telemetry only. GPU figures below are GPU-only and are <b>not</b> part of or equal to this total.</div>
    </div>`;
  }

  // ---- GPU energy section ---------------------------------------------------
  function gpuSection(g) {
    const s = g && g.sampler || {};
    const today = g && g.today || {};
    const h24 = g && g.h24 || {};
    const month = g && g.month || {};
    const state = s.running
      ? '<span class="right status-pill status-on">sampler running</span>'
      : '<span class="right status-pill status-off">sampler stopped' + (s.last_error ? " — " + esc(s.last_error) : "") + '</span>';
    return `<div class="card">
      <div class="card-title">GPU energy ${state}</div>
      <div class="grid grid-4">
        ${tile("GPU current power", gpuCurrentW())}
        ${tile("GPU average power", h24.average_power_w != null ? h24.average_power_w.toFixed(1) + " W" : "—", "24 h avg")}
        ${tile("GPU energy today", fmtKwh(today), costLine(today))}
        ${tile("GPU energy 24h", fmtKwh(h24), costLine(h24))}
      </div>
      <div class="grid grid-3" style="margin-top:10px">
        ${tile("GPU energy 30d", fmtKwh(month), costLine(month))}
        ${tile("Measured time 24h", h24.measured_seconds != null ? Math.round(h24.measured_seconds) + " s" : "—", "gaps excluded")}
        ${tile("Energy basis", "calculated", "avg W × measured interval / 3600")}
      </div>
      <div class="text-faint" style="font-size:12px;margin-top:10px">
        GPU-only telemetry (NVML, 0.5 s RAM-buffered samples → one aggregated DB point per 10 s).
        <b>GPU energy is NOT total server energy.</b>
        ${s.last_error && s.running ? " Last sampler error: " + esc(s.last_error) : ""}
      </div>
      <div id="elec-gpu-periods" style="margin-top:14px"></div>
      <div id="elec-gpu-detail" style="margin-top:10px"></div>
    </div>`;
  }

  function gpuCurrentW() {
    const snap = App.state.snapshot;
    const gpus = snap && snap.gpu && snap.gpu.gpus || [];
    let total = null;
    const rows = gpus.map((g) => {
      const w = g.power_draw;
      if (w != null) total = (total || 0) + w;
      return `<div class="detail-grid"><div class="k">GPU ${g.index ?? "?"} ${esc(g.name || "")}</div>
        <div class="v mono">${w != null ? w.toFixed(1) + " W" : "—"}</div></div>`;
    }).join("");
    return rows
      ? `<div class="mono" style="font-size:18px">${total != null ? total.toFixed(1) + " W" : "—"}</div>`
      : '<div class="mono" style="font-size:18px">—</div>';
  }

  function fmtKwh(win) {
    return win && win.kwh != null ? win.kwh.toFixed(4) + " kWh" : "—";
  }

  function costLine(win) {
    if (!win) return "";
    if (win.cost != null) return "cost " + win.cost.toFixed(2) + " " + esc(win.currency || "lei") + " (calculated)";
    if (win.cost_notice) return "cost unavailable — tariff not configured";
    return "";
  }

  function tile(label, value, sub) {
    return `<div class="metric-tile"><div class="metric-label">${label}</div>
      <div class="metric-value mono" style="font-size:18px">${value}</div>
      ${sub ? `<div class="metric-sub">${sub}</div>` : ""}</div>`;
  }

  async function refreshGpuEnergy() {
    let g;
    try { g = await API.get("/api/electricity/gpu-energy"); }
    catch { return; }
    const holder = document.getElementById("elec-gpu-card");
    if (holder) holder.innerHTML = gpuSection(g);
    await renderGpuPeriods(g);
    renderGpuDetail(g);
    drawGpuChart();
  }

  async function renderGpuPeriods(g) {
    const box = document.getElementById("elec-gpu-periods");
    if (!box) return;
    const periods = [["7d", 10080], ["30d", 43200]];
    let html = '<div class="grid grid-2">';
    for (const [label, minutes] of periods) {
      let s;
      try { s = await API.get(`/api/electricity/gpu-energy-window?minutes=${minutes}`); }
      catch { s = {}; }
      html += gpuPeriodCard(label, s);
    }
    html += "</div>";
    box.innerHTML = html;
  }

  function gpuPeriodCard(label, s) {
    let body;
    if (s.kwh == null) {
      body = `<div class="metric-value mono">—</div>
        <div class="text-faint" style="font-size:12px">${s.points ? "no complete measured intervals in window" : "no data in window"}</div>`;
    } else {
      body = `<div class="metric-value mono">${s.kwh.toFixed(4)} kWh</div>
        <div class="text-faint" style="font-size:12px">${s.average_power_w != null ? "avg " + s.average_power_w.toFixed(1) + " W · " : ""}${Math.round(s.measured_seconds || 0)} s measured</div>
        ${s.cost != null ? `<div style="margin-top:4px"><span class="mono">${s.cost.toFixed(2)} ${esc(s.currency || "lei")}</span> <span class="text-faint" style="font-size:11px">(calculated: kWh × tariff)</span></div>`
          : `<div class="text-faint" style="font-size:12px;margin-top:4px">cost unavailable — tariff not configured</div>`}`;
    }
    return `<div class="metric-tile"><div class="metric-label">GPU energy · ${label}</div>${body}</div>`;
  }

  function renderGpuDetail(g) {
    const box = document.getElementById("elec-gpu-detail");
    if (!box || !g || !g.h24) return;
    const rows = (g.h24.gpus || []).map((x) =>
      `<div class="detail-grid"><div class="k">GPU ${x.gpu_index}</div>
       <div class="v mono">${x.kwh != null ? x.kwh.toFixed(4) + " kWh · " + Math.round(x.measured_seconds) + " s" : "—"}</div></div>`).join("");
    box.innerHTML = rows ? `<div class="text-faint" style="font-size:12px;margin-bottom:4px">Per-GPU split (24 h):</div>${rows}` : "";
  }

  let gpuChart = null;
  async function drawGpuChart() {
    let hist;
    try { hist = await API.get("/api/electricity/gpu-history?minutes=60"); } catch { return; }
    const pts = (hist.points || []).filter((p) => p.data && p.data.average_power_w != null);
    const wrap = document.getElementById("elec-gpu-chart");
    if (!wrap) return;
    if (!pts.length) {
      const holder = wrap.parentElement || wrap;
      holder.innerHTML = '<div class="empty text-dim">no aggregated GPU energy points yet — one point appears every 10 s while the sampler runs</div>';
      return;
    }
    gpuChart = seriesChart("elec-gpu-chart", "GPU W (avg)", (p) => p.data.average_power_w, "#f5a623");
    gpuChart.load(pts, (p) => p.data.average_power_w);
  }

  // ---- tariff config ----------------------------------------------------------
  async function loadConfig() {
    try { return await API.get("/api/electricity/config"); }
    catch { return { tariff_is_configured: false, notice: "config unavailable (admin only)" }; }
  }

  function renderConfig(cfg) {
    const box = document.getElementById("elec-config");
    if (!box) return;
    box.innerHTML = `
      ${cfg.notice ? `<div class="alert warn" style="margin-bottom:10px">${esc(cfg.notice)}</div>` : ""}
      <div class="detail-grid">
        <div class="k">Tariff</div><div class="v mono">${cfg.tariff_is_configured ? cfg.tariff + " lei/kWh" : "not configured"}</div>
        <div class="k">Currency</div><div class="v mono">${esc(cfg.currency || "lei")}</div>
      </div>
      <div style="margin-top:10px;display:flex;gap:8px;align-items:center">
        <input id="elec-tariff" type="number" min="0" step="0.01" placeholder="tariff, lei/kWh"
               value="${cfg.tariff_is_configured ? cfg.tariff : ""}" style="max-width:180px">
        <button class="btn" id="elec-save">Save</button>
        <span class="text-faint" style="font-size:12px">admin only · stored in settings, no default invented</span>
      </div>`;
    box.querySelector("#elec-save").addEventListener("click", saveConfig);
  }

  async function saveConfig() {
    const input = document.getElementById("elec-tariff");
    const raw = input.value.trim();
    const payload = {};
    if (raw !== "") {
      const v = Number(raw);
      if (!Number.isFinite(v) || v < 0) { toast("tariff must be a non-negative number", "error"); return; }
      payload.tariff = v;
    } else {
      payload.tariff = 0; // explicit "clear tariff" → not configured
    }
    try {
      const cfg = await API.put("/api/electricity/config", payload);
      renderConfig(cfg);
      refreshGpuEnergy();
      toast("tariff saved", "success");
    } catch (e) { toast("save failed: " + e.message, "error"); }
  }

  function wireActions() {
    loadConfig().then(renderConfig);
  }

  function build(data) {
    return `
      ${liveSection(data)}
      <div id="elec-gpu-card"><div class="card"><div class="card-title">GPU energy</div><div class="empty text-dim">loading…</div></div></div>
      <div class="card">
        <div class="card-title">GPU average power — last 60 min</div>
        <div style="height:200px"><canvas id="elec-gpu-chart"></canvas></div>
      </div>
      <div class="card">
        <div class="card-title">Tariff settings</div>
        <div id="elec-config"><div class="text-dim">loading…</div></div>
      </div>`;
  }

  window.Pages = window.Pages || {};
  window.Pages.electricity = { render, destroy, onSnapshot() {}, tick };
})();
