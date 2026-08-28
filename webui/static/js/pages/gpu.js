/* GPU page — full GPU details + processes */
(function () {
  async function render(el) {
    let gpu;
    try { gpu = await API.get("/api/gpu"); } catch (e) { gpu = { available: false, reason: e.message }; }
    el.innerHTML = build(gpu);
    if (gpu.available) draw(gpu);
    // processes
    try {
      const procs = await API.get("/api/gpu/processes");
      drawProcesses(procs.processes || [], gpu);
    } catch (e) { console.error(e); }
  }

  function build(gpu) {
    if (!gpu.available || !gpu.gpus || !gpu.gpus.length) {
      return `<div class="card">
        <div class="card-title">GPU Monitoring</div>
        <div class="empty">
          <div class="big">🎮</div>
          ${esc(gpu.reason || "No NVIDIA GPU detected")}
          <div class="text-faint" style="margin-top:8px">nvidia-smi / NVML is not available on this host.<br/>Deploy WebOllama on the machine with the Tesla V100 (or mount a GPU collector) to see live utilization, VRAM, temperature, power, fan control and clocks.</div>
        </div>
      </div>`;
    }
    return `<div id="gpu-cards"></div>
      <div class="card">
        <div class="card-title">GPU Processes</div>
        <div id="gpu-procs"><div class="empty text-dim">loading…</div></div>
      </div>`;
  }

  function draw(gpu) {
    const cards = document.getElementById("gpu-cards");
    cards.innerHTML = gpu.gpus.map((g, i) => {
      const vramP = g.vram_total ? ((g.vram_used || 0) / g.vram_total) * 100 : 0;
      const util = g.utilization ?? null;
      return `
      <div class="card">
        <div class="card-title">GPU ${i} · ${esc(g.name)} <span class="right"><span class="status-pill ${util >= 90 ? "status-off" : util >= 60 ? "status-warn" : "status-on"}">${util != null ? util.toFixed(0) + "% util" : "idle"}</span></span></div>
        <div class="grid grid-2" style="margin-bottom:14px">
          <div>
            <div class="metric-label" style="margin-bottom:4px">GPU Utilization</div>
            <div class="gpu-bar"><div class="bar"><div style="width:${(util ?? 0).toFixed(0)}%;background:${gpuPctColor(util)}"></div></div><span class="pct">${util != null ? util.toFixed(0) + "%" : "—"}</span></div>
          </div>
          <div>
            <div class="metric-label" style="margin-bottom:4px">VRAM ${fmtBytes(g.vram_used)} / ${fmtBytes(g.vram_total)}</div>
            <div class="gpu-bar"><div class="bar"><div style="width:${vramP.toFixed(0)}%;background:var(--accent)"></div></div><span class="pct">${vramP.toFixed(0)}%</span></div>
          </div>
        </div>
        <div class="grid grid-4">
          ${tile("Temperature", (g.temperature ?? "—") + "°C", g.temperature)}
          ${tile("Power Draw", (g.power_draw ?? "—") + " W", g.power_draw)}
          ${tile("Power Limit", (g.power_limit ?? "—") + " W")}
          ${g.fan_available
            ? tile("Fan Target", (g.fan_target ?? "—") + "%", g.fan_target) + tile("Fan PWM", (g.fan_pwm ?? "—") + "%", g.fan_pwm)
            : `<div class="metric-tile"><div class="metric-label">Fan Target</div><div class="metric-value mono" style="font-size:16px">—</div></div><div class="metric-tile"><div class="metric-label">Fan PWM</div><div class="metric-value mono" style="font-size:16px">—</div></div><div class="metric-tile" style="grid-column:1/-1"><div class="metric-label">Fan Status</div><div class="metric-sub">unavailable (v100-fan.service)</div></div>`}
          ${tile("Clock (SM)", (g.clocks ?? "—") + " MHz")}
          ${tile("Mem Clock", (g.mem_clock ?? "—") + " MHz")}
          ${tile("VRAM", fmtBytes(g.vram_used) + " / " + fmtBytes(g.vram_total))}
          ${tile("Memory Util", (g.memory_utilization ?? "—") + "%")}
        </div>
        <div class="detail-grid" style="margin-top:14px">
          <div class="k">Driver</div><div class="v mono">${esc(g.driver_version || "—")}</div>
          <div class="k">CUDA</div><div class="v mono">${esc(g.cuda_version || "—")}</div>
          <div class="k">PCIe</div><div class="v mono">${g.pcie_gen != null ? `Gen ${g.pcie_gen} ×${g.pcie_width ?? "—"}` : "—"}${g.pcie_gen_max != null ? ` (max Gen ${g.pcie_gen_max} ×${g.pcie_width_max ?? "—"})` : ""}</div>
          <div class="k">Bus</div><div class="v mono">${esc(g.pci_bus || "—")}</div>
          <div class="k">P-State</div><div class="v mono">${esc(g.pstate || "—")}</div>
          <div class="k">Compute Mode</div><div class="v mono">${esc(g.compute_mode || "—")}</div>
          <div class="k">Persistence</div><div class="v mono">${esc(g.persistence_mode || "—")}</div>
          <div class="k">Display</div><div class="v mono">${esc(g.display_active || "—")}</div>
        </div>
      </div>`;
    }).join("");
  }

  function tile(label, value, danger) {
    const cls = danger != null && danger >= 80 ? "warn" : "";
    return `<div class="metric-tile ${cls}"><div class="metric-label">${label}</div><div class="metric-value mono" style="font-size:16px">${value}</div></div>`;
  }

  function drawProcesses(procs, gpu) {
    const el = document.getElementById("gpu-procs");
    if (!el) return;
    if (!procs.length) {
      el.innerHTML = '<div class="empty text-dim">No compute processes on GPU' + (gpu && gpu.gpus && gpu.gpus[0] ? " (" + esc(gpu.gpus[0].name) + ")" : "") + '</div>';
      return;
    }
    el.innerHTML = `<div style="overflow-x:auto"><table><thead><tr><th>PID</th><th>Process</th><th class="num">GPU Memory</th><th>GPU</th></tr></thead>
      <tbody>${procs.map((p) => `<tr>
        <td class="mono">${p.pid}</td>
        <td class="mono" style="word-break:break-all">${esc(p.name || p.process_name || "—")}${(p.name || "").toLowerCase().includes("ollama") ? ' <span class="badge loaded">ollama</span>' : ""}</td>
        <td class="num mono">${fmtBytes(p.used_memory != null ? p.used_memory : p.usedGpuMemory)}</td>
        <td class="mono text-faint">${p.gpu_index != null ? p.gpu_index : p.gpu_uuid ? esc(p.gpu_uuid.slice(0, 8)) : "—"}</td>
      </tr>`).join("")}</tbody></table></div>`;
  }

  function onSnapshot(snap) {
    if (App.state.currentPage !== "gpu") return;
    const gpu = snap.gpu || {};
    if (gpu.available && gpu.gpus && gpu.gpus.length) {
      const cards = document.getElementById("gpu-cards");
      if (cards) draw(gpu);
    }
  }

  window.Pages = window.Pages || {};
  window.Pages.gpu = { render, onSnapshot };
})();