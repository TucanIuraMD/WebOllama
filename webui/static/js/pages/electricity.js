/* Electricity page — host power telemetry, energy history, cost estimate.
 *
 * Data honesty contract (mirrors the backend):
 *  - `watts` is shown only when a HOST-level source (RAPL / hwmon) exists.
 *  - GPU power draw is displayed ONLY as a per-GPU reference and is clearly
 *    labeled "not total server power".
 *  - kWh is CALCULATED (integration), cost is CALCULATED (kWh × tariff);
 *    both are labeled as such, never as measurements.
 *  - No tariff configured → explicit "needs configuration" notice, no
 *    invented default.
 * Polls /api/electricity every 5s with a page-scoped timer (same pattern as
 * the Dashboard: single setInterval, cleared on re-render).
 */
(function () {
  let elecTimer = null;
  let chart = null;

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
    wireActions(el);
    drawChart();
    const cfg = await loadConfig();
    renderConfig(cfg);
    elecTimer = setInterval(refresh, 5000);
  }

  function destroy() {
    if (elecTimer) { clearInterval(elecTimer); elecTimer = null; }
  }

  async function refresh() {
    if (App.state.currentPage !== "electricity") { destroy(); return; }
    let data;
    try { data = await API.get("/api/electricity"); } catch { return; }
    const sec = document.getElementById("elec-live");
    if (sec) sec.outerHTML = liveSection(data);
  }

  function build(data) {
    return `
      ${liveSection(data)}
      <div class="card">
        <div class="card-title">Energy history <span class="right text-faint" style="font-size:12px" id="elec-hist-meta"></span></div>
        <div style="height:220px"><canvas id="elec-chart"></canvas></div>
        <div id="elec-periods" style="margin-top:14px"></div>
      </div>
      <div class="card">
        <div class="card-title">Tariff settings</div>
        <div id="elec-config"><div class="text-dim">loading…</div></div>
      </div>`;
  }

  function liveSection(data) {
    if (!data.available) {
      return `<div class="card" id="elec-live">
        <div class="card-title">Current power</div>
        <div class="empty">
          <div class="big">⚡</div>
          <b>Total server power: data unavailable</b>
          <div class="text-faint" style="margin-top:8px;max-width:640px">${esc(data.reason || "no host-level power telemetry source on this machine")}</div>
          ${gpuRef(data)}
        </div>
      </div>`;
    }
    const c = data.components || {};
    return `<div class="card" id="elec-live">
      <div class="card-title">Current power <span class="right status-pill status-on">measured · ${esc(data.source || "")}</span></div>
      <div class="grid grid-4">
        ${tile("Total server power", data.watts != null ? data.watts.toFixed(1) + " W" : "—")}
        ${tile("CPU (RAPL)", c.cpu && c.cpu.watts != null ? c.cpu.watts.toFixed(1) + " W" : "—")}
        ${tile("Platform (hwmon)", c.platform && c.platform.watts != null ? c.platform.watts.toFixed(1) + " W" : "—")}
        ${tile("GPU draw (reference)", data.gpu_power && data.gpu_power.watts != null ? data.gpu_power.watts.toFixed(1) + " W" : "—")}
      </div>
      <div class="text-faint" style="font-size:12px;margin-top:10px">GPU value is the sum of per-GPU power_draw — <b>not</b> total server power. Energy below is calculated by integrating measured power over time.</div>
    </div>`;
  }

  function gpuRef(data) {
    const gp = data.gpu_power;
    if (!gp || gp.watts == null) return "";
    const rows = (gp.per_device || []).map((d) =>
      `<div class="detail-grid"><div class="k">GPU ${d.index ?? "?"} ${esc(d.name || "")}</div>
       <div class="v mono">${d.watts != null ? d.watts.toFixed(1) + " W" : "—"}</div></div>`).join("");
    return `<div style="margin-top:12px;max-width:640px;text-align:left">
      <div class="text-faint" style="font-size:12px;margin-bottom:6px">GPU power draw (measured, reference only — NOT total server power):</div>${rows}</div>`;
  }

  function tile(label, value) {
    return `<div class="metric-tile"><div class="metric-label">${label}</div>
      <div class="metric-value mono" style="font-size:18px">${value}</div></div>`;
  }

  async function drawChart() {
    let hist;
    try { hist = await API.get("/api/electricity/history?minutes=60"); } catch { return; }
    const pts = (hist.points || []).filter((p) => p.data && p.data.watts != null);
    const meta = document.getElementById("elec-hist-meta");
    const wrap = document.getElementById("elec-chart");
    if (!pts.length) {
      if (wrap) {
        // replace the chart's sizing wrapper with an honest empty state
        const holder = wrap.parentElement || wrap;
        holder.innerHTML = '<div class="empty text-dim">no electricity history yet — samples appear every 5s while a host-level source is available</div>';
      }
      if (meta) meta.textContent = "";
    } else if (wrap) {
      chart = seriesChart("elec-chart", "W", (p) => p.data.watts, "#f5a623");
      chart.load(pts, (p) => p.data.watts);
      if (meta) meta.textContent = `${pts.length} samples · 60 min`;
    }
    renderPeriods();
  }

  async function renderPeriods() {
    const box = document.getElementById("elec-periods");
    if (!box) return;
    const periods = [["24h", 1440], ["7d", 10080], ["30d", 43200]];
    let html = '<div class="grid grid-3">';
    for (const [label, minutes] of periods) {
      let s;
      try { s = await API.get(`/api/electricity/summary?minutes=${minutes}`); }
      catch { s = { kwh: null }; }
      html += periodCard(label, s);
    }
    html += "</div>";
    box.innerHTML = html;
  }

  function periodCard(label, s) {
    const bounded = s.window_bounded_by_retention
      ? `<div class="text-faint" style="font-size:11px">window limited by metrics retention (${Math.round((s.retention_seconds || 0) / 3600)} h)</div>` : "";
    let body;
    if (s.kwh == null) {
      body = `<div class="metric-value mono">—</div>
        <div class="text-faint" style="font-size:12px">${s.points ? "no complete intervals in window" : "no data in window"}</div>`;
    } else {
      const cost = (s.tariff_configured && s.cost != null)
        ? `<div style="margin-top:6px"><span class="metric-label">Cost</span> <span class="mono">${s.cost.toFixed(2)} ${esc(s.currency || "lei")}</span> <span class="text-faint" style="font-size:11px">(calculated: kWh × tariff)</span></div>`
        : `<div class="text-faint" style="font-size:12px;margin-top:6px">cost unavailable — tariff not configured</div>`;
      body = `<div class="metric-value mono">${s.kwh.toFixed(4)} kWh</div>
        <div class="text-faint" style="font-size:12px">calculated · ${s.measured ? "from measured power" : "partially estimated"}</div>${cost}`;
    }
    return `<div class="metric-tile"><div class="metric-label">Consumption · ${label}</div>${body}${bounded}</div>`;
  }

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
      renderPeriods();
      toast("tariff saved", "success");
    } catch (e) { toast("save failed: " + e.message, "error"); }
  }

  function wireActions() { /* current actions wired inline */ }

  window.Pages = window.Pages || {};
  window.Pages.electricity = { render, destroy, onSnapshot() {} };
})();
