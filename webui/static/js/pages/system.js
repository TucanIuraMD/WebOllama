/* System page — CPU / RAM / disk / network / processes / ollama process */
(function () {
  let charts = null;

  async function render(el) {
    Charts.destroyAll("sys");
    let data;
    try { data = await API.get("/api/system"); } catch (e) { data = null; }
    el.innerHTML = build(data);
    if (data) draw(data);
    // live updates
  }

  function build(d) {
    return `
      <div class="grid grid-4" id="sys-tiles"></div>
      <div class="card">
        <div class="card-title">CPU &amp; RAM History</div>
        <div class="grid grid-2">
          <div class="chart-box"><canvas id="sys-chart-cpu"></canvas></div>
          <div class="chart-box"><canvas id="sys-chart-ram"></canvas></div>
        </div>
      </div>
      <div class="card">
        <div class="card-title">Network</div>
        <div class="chart-box" style="height:150px"><canvas id="sys-chart-net"></canvas></div>
        <div id="sys-netifaces" style="margin-top:10px"></div>
      </div>
      <div class="grid grid-2">
        <div class="card">
          <div class="card-title">Top CPU Processes</div>
          <div id="sys-topcpu"></div>
        </div>
        <div class="card">
          <div class="card-title">Top Memory Processes</div>
          <div id="sys-topmem"></div>
        </div>
      </div>
      <div class="card">
        <div class="card-title">Ollama Process</div>
        <div id="sys-ollama-proc"></div>
      </div>`;
  }

  function draw(d) {
    const c = d.cpu || {}, r = d.ram || {}, s = d.swap || {}, disk = d.disk || {}, net = d.network || {};
    const tiles = document.getElementById("sys-tiles");
    tiles.innerHTML = `
      <div class="metric-tile">
        <div class="metric-label">CPU Usage</div>
        <div class="metric-value">${(c.percent ?? 0).toFixed(0)}%</div>
        <div class="metric-sub">${c.cores ?? 0} cores / ${c.threads ?? 0} threads</div>
      </div>
      <div class="metric-tile">
        <div class="metric-label">Load 1/5/15</div>
        <div class="metric-value mono" style="font-size:18px">${(c.load_1 ?? 0).toFixed(2)} / ${(c.load_5 ?? 0).toFixed(2)} / ${(c.load_15 ?? 0).toFixed(2)}</div>
        <div class="metric-sub">CPU freq ${c.frequency && c.frequency.current ? c.frequency.current.toFixed(0) + " MHz" : "—"}</div>
      </div>
      <div class="metric-tile">
        <div class="metric-label">RAM</div>
        <div class="metric-value">${(r.percent ?? 0).toFixed(0)}%</div>
        <div class="metric-sub">${fmtBytes(r.used)} / ${fmtBytes(r.total)} · cached ${fmtBytes(r.cached)}</div>
      </div>
      <div class="metric-tile">
        <div class="metric-label">Swap</div>
        <div class="metric-value">${(s.percent ?? 0).toFixed(0)}%</div>
        <div class="metric-sub">${fmtBytes(s.used)} / ${fmtBytes(s.total)}</div>
      </div>
      <div class="metric-tile">
        <div class="metric-label">Disk</div>
        <div class="metric-value">${(disk.percent ?? 0).toFixed(0)}%</div>
        <div class="metric-sub">${fmtBytes(disk.used)} / ${fmtBytes(disk.total)} free ${fmtBytes(disk.free)}</div>
      </div>
      <div class="metric-tile">
        <div class="metric-label">Network RX / TX</div>
        <div class="metric-value mono" style="font-size:16px">${fmtSpeed(net.rx_rate)}</div>
        <div class="metric-sub">↓ RX · ↑ TX ${fmtSpeed(net.tx_rate)}</div>
      </div>
      <div class="metric-tile">
        <div class="metric-label">Uptime</div>
        <div class="metric-value mono" style="font-size:16px">${fmtClock(d.uptime)}</div>
        <div class="metric-sub">${esc(d.hostname || "")} · ${esc(d.os || "")}</div>
      </div>`;

    // charts
    charts = {
      cpu: seriesChart("sys-chart-cpu", "CPU %", (p) => p, null),
      ram: seriesChart("sys-chart-ram", "RAM used %", (p) => p, "#9b7bff"),
      net: seriesChart("sys-chart-net", "Network MB/s", (p) => p, "#35c48a"),
    };
    // seed from history
    loadHistory();

    // net ifaces
    const ifaceEl = document.getElementById("sys-netifaces");
    if (net.interfaces) {
      ifaceEl.innerHTML = Object.entries(net.interfaces).map(([name, i]) => `
        <div style="display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid var(--border);font-size:13px">
          <span class="mono">${esc(name)}</span>
          <span class="mono text-dim">↓ ${fmtSpeed(i.rx_rate)} · ↑ ${fmtSpeed(i.tx_rate)}</span>
          <span class="mono text-faint">${fmtBytes(i.rx)} / ${fmtBytes(i.tx)}</span>
        </div>`).join("");
    }

    // processes
    const cpuEl = document.getElementById("sys-topcpu");
    const memEl = document.getElementById("sys-topmem");
    if (d.processes) {
      cpuEl.innerHTML = procTable(d.processes.top_cpu || []);
      memEl.innerHTML = procTable(d.processes.top_memory || []);
    }

    // ollama proc
    const opEl = document.getElementById("sys-ollama-proc");
    API.get("/api/system").then((fresh) => {
      // use snapshot for ollama process instead
    }).catch(() => {});
    loadOllamaProc(opEl);
  }

  function procTable(procs) {
    if (!procs.length) return '<div class="empty text-dim">no data</div>';
    return `<table><thead><tr><th>PID</th><th>Name</th><th class="num">CPU%</th><th class="num">MEM%</th><th class="num">RSS</th></tr></thead>
      <tbody>${procs.map((p) => `<tr>
        <td class="mono text-faint">${p.pid}</td>
        <td class="mono ellipsis" style="max-width:200px">${esc(p.name)}</td>
        <td class="num mono">${(p.cpu || 0).toFixed(1)}</td>
        <td class="num mono">${(p.memory || 0).toFixed(1)}</td>
        <td class="num mono">${fmtBytes(p.rss)}</td>
      </tr>`).join("")}</tbody></table>`;
  }

  async function loadOllamaProc(el) {
    try {
      const snap = App.state.snapshot;
      if (snap && snap.system && snap.system.ollama_proc) {
        el.innerHTML = renderOllamaProc(snap.system.ollama_proc);
        return;
      }
    } catch {}
    el.innerHTML = '<div class="text-dim">fetching…</div>';
  }

  function renderOllamaProc(p) {
    return `<div class="detail-grid">
      <div class="k">PID</div><div class="v mono">${p.pid || "—"}</div>
      <div class="k">Name</div><div class="v mono">${esc(p.name || "—")}</div>
      <div class="k">CPU</div><div class="v mono">${p.cpu != null ? p.cpu.toFixed(1) + "%" : "—"}</div>
      <div class="k">Memory</div><div class="v mono">${p.memory != null ? p.memory.toFixed(1) + "% (" + fmtBytes(p.rss) + ")" : "—"}</div>
      <div class="k">Status</div><div class="v mono">${esc(p.status || "—")}</div>
      <div class="k">Started</div><div class="v mono">${p.create_time ? new Date(p.create_time * 1000).toLocaleString() : "—"}</div>
    </div>`;
  }

  async function loadHistory() {
    try {
      const h = await API.get("/api/history/system?minutes=60");
      const points = h.points || [];
      if (charts) {
        charts.cpu.load(points, (p) => p.data && p.data.cpu != null ? p.data.cpu : null);
        charts.ram.load(points, (p) => p.data && p.data.ram_used != null && p.data.ram_total ? (p.data.ram_used / p.data.ram_total) * 100 : null);
        charts.net.load(points, (p) => p.data && p.data.rx_rate != null ? (p.data.rx_rate + (p.data.tx_rate || 0)) / (1024 ** 2) : null);
      }
    } catch (e) { console.error(e); }
  }

  function onSnapshot(snap) {
    if (App.state.currentPage !== "system") return;
    if (!snap.cpu) return;
    const tiles = document.getElementById("sys-tiles");
    if (!tiles) return;
    draw({ cpu: snap.cpu, ram: snap.ram, swap: snap.swap, disk: snap.disk, network: snap.network, uptime: snap.system && snap.system.uptime, hostname: snap.system && snap.system.hostname, os: snap.system && snap.system.os });
  }

  window.Pages = window.Pages || {};
  window.Pages.system = { render, onSnapshot };
})();