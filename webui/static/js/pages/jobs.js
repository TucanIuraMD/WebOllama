/* Jobs page — long-running operations with live progress */
(function () {
  let jobs = [];
  let autoRefresh = true;

  async function render(el) {
    el.innerHTML = `
      <div class="card">
        <div class="card-title">Jobs <span class="text-dim" id="jobs-count"></span>
          <span class="right">
            <button class="btn-sm" id="jobs-refresh">⟳ Refresh</button>
            <button class="btn-sm" id="jobs-autorefresh">⏸ Pause live</button>
          </span>
        </div>
        <div id="jobs-body"></div>
      </div>`;
    document.getElementById("jobs-refresh").onclick = load;
    document.getElementById("jobs-autorefresh").onclick = () => {
      autoRefresh = !autoRefresh;
      document.getElementById("jobs-autorefresh").textContent = autoRefresh ? "⏸ Pause live" : "▶ Resume live";
    };
    await load();
    // periodic refresh when not using WS
    setInterval(() => { if (autoRefresh && App.state.currentPage === "jobs") load(); }, 5000);
  }

  async function load() {
    try {
      const data = await API.get("/api/jobs?limit=50");
      jobs = data.jobs || [];
      draw();
    } catch (e) { console.error(e); }
  }

  function draw() {
    const count = document.getElementById("jobs-count");
    const body = document.getElementById("jobs-body");
    if (!count || !body) return;
    count.textContent = jobs.length;
    if (!jobs.length) {
      body.innerHTML = '<div class="empty"><div class="big">⚙</div>No jobs yet</div>';
      return;
    }
    body.innerHTML = `
      <div style="overflow-x:auto">
        <table>
          <thead><tr>
            <th>ID</th><th>Operation</th><th>Model</th><th>Status</th><th class="num">Progress</th><th>Started</th><th>Duration</th><th>Output</th><th></th>
          </tr></thead>
          <tbody>${jobs.map((j) => {
            const st = j.status;
            const cls = st === "complete" ? "status-on" : st === "error" ? "status-off" : st === "cancelled" ? "status-warn" : "status-idle";
            const running = st === "running" || st === "pending";
            return `<tr>
              <td class="mono text-faint">${esc(j.id)}</td>
              <td><span class="badge">${esc(j.operation)}</span></td>
              <td class="mono ellipsis">${esc(j.model || "—")}</td>
              <td><span class="status-pill ${cls}">${running ? '<span class="spin">⟳</span> ' : ""}${esc(st)}</span></td>
              <td class="num">
                <div style="min-width:120px">
                  <div class="progress ${st === "error" ? "red" : st === "complete" ? "green" : ""}"><div style="width:${(j.progress || 0).toFixed(0)}%"></div></div>
                  <span class="text-faint" style="font-size:11px">${j.progress ? j.progress.toFixed(0) + "%" : ""} ${j.speed ? "· " + fmtSpeed(j.speed) : ""}</span>
                </div>
              </td>
              <td class="text-dim nowrap">${new Date((j.started_at || 0) * 1000).toLocaleString()}</td>
              <td class="text-dim mono">${j.finished_at ? fmtDuration(j.duration) : running ? "…" : "—"}</td>
              <td class="mono text-faint ellipsis" style="max-width:180px" title="${esc(j.output)}">${esc((j.output || "").split("\n").slice(-1)[0] || "")}</td>
              <td class="nowrap">
                ${running ? `<button class="btn-sm btn-danger" data-action="job-cancel" data-job="${esc(j.id)}">⏹</button>` : ""}
                <button class="btn-sm" data-action="job-detail" data-job="${esc(j.id)}">👁</button>
              </td>
            </tr>`;
          }).join("")}</tbody>
        </table>
      </div>`;
  }

  function onJobUpdate(job) {
    const idx = jobs.findIndex((j) => j.id === job.id);
    if (idx >= 0) jobs[idx] = job; else jobs.unshift(job);
    if (App.state.currentPage === "jobs") draw();
  }

  window.Actions = window.Actions || {};
  window.Actions["job-cancel"] = async (data) => {
    try {
      await API.post(`/api/jobs/${data.job}/cancel`);
      toast("Cancelling job " + data.job, "info");
      load();
    } catch (e) { toast(e.message, "error"); }
  };
  window.Actions["job-detail"] = async (data) => {
    try {
      const j = await API.get(`/api/jobs/${data.job}`);
      openModal(`
        <div class="detail-grid">
          <div class="k">ID</div><div class="v mono">${esc(j.id)}</div>
          <div class="k">Operation</div><div class="v">${esc(j.operation)}</div>
          <div class="k">Model</div><div class="v mono">${esc(j.model)}</div>
          <div class="k">Status</div><div class="v">${esc(j.status)}</div>
          <div class="k">Progress</div><div class="v">${(j.progress || 0).toFixed(1)}%</div>
          <div class="k">Started</div><div class="v">${new Date((j.started_at || 0) * 1000).toLocaleString()}</div>
          <div class="k">Duration</div><div class="v mono">${fmtDuration(j.duration)}</div>
          ${j.error ? `<div class="k">Error</div><div class="v" style="color:var(--red)">${esc(j.error)}</div>` : ""}
        </div>
        ${j.output ? `<div class="card-title" style="margin-top:12px">Output</div><pre style="background:#05070b;padding:10px;border-radius:6px;max-height:300px;overflow:auto" class="mono">${esc(j.output)}</pre>` : ""}
      `, { title: "Job " + j.id });
    } catch (e) { toast(e.message, "error"); }
  };

  window.Pages = window.Pages || {};
  window.Pages.jobs = { render, onJobUpdate };
})();