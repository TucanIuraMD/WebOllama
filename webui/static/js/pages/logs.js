/* Logs page — Web UI logs, job logs, Ollama systemd logs */
(function () {
  let source = "webui";
  let timer = null;

  async function render(el) {
    el.innerHTML = `
      <div class="card">
        <div class="card-title">Logs
          <span class="right">
            <select id="log-source" style="max-width:160px">
              <option value="webui">Web UI logs</option>
              <option value="ollama">Ollama (systemd)</option>
              <option value="job">Job logs</option>
            </select>
            <input type="text" id="log-search" placeholder="filter…" style="max-width:160px" />
            <button class="btn-sm" id="log-tail">⏷ Tail ${tail}</button>
            <button class="btn-sm" id="log-clear">✕ Clear view</button>
          </span>
        </div>
        <div id="log-job-picker" style="display:none;margin-bottom:10px">
          <select id="log-job-select" style="max-width:300px"><option value="">— choose job —</option></select>
        </div>
        <div class="log-view" id="log-view">loading…</div>
      </div>`;
    tail = 100;
    document.getElementById("log-source").onchange = () => {
      source = document.getElementById("log-source").value;
      document.getElementById("log-job-picker").style.display = source === "job" ? "" : "none";
      if (source === "job") loadJobPicker();
      load();
    };
    document.getElementById("log-search").oninput = debounce(load, 300);
    document.getElementById("log-tail").onclick = () => {
      tail = tail === 100 ? 500 : tail === 500 ? 1000 : tail === 1000 ? 2000 : 100;
      document.getElementById("log-tail").textContent = "⏷ Tail " + tail;
      load();
    };
    document.getElementById("log-clear").onclick = () => {
      document.getElementById("log-view").textContent = "";
    };
    document.getElementById("log-job-select").onchange = () => { load(); };
    load();
    clearInterval(timer);
    timer = setInterval(() => { if (App.state.currentPage === "logs") load(); }, 5000);
  }

  let tail = 100;

  async function loadJobPicker() {
    try {
      const data = await API.get("/api/jobs?limit=100");
      const sel = document.getElementById("log-job-select");
      sel.innerHTML = '<option value="">— choose job —</option>' + (data.jobs || []).map((j) =>
        `<option value="${esc(j.id)}">${esc(j.id)} · ${esc(j.operation)} · ${esc(j.model || "")}</option>`).join("");
    } catch (e) { console.error(e); }
  }

  async function load() {
    const view = document.getElementById("log-view");
    if (!view) return;
    const search = document.getElementById("log-search").value;
    try {
      let lines = [];
      if (source === "webui") {
        const data = await API.get(`/api/logs/webui?tail=${tail}&search=${encodeURIComponent(search)}`);
        lines = data.lines || [];
      } else if (source === "ollama") {
        const data = await API.get(`/api/logs/ollama?tail=${tail}&search=${encodeURIComponent(search)}`);
        lines = data.lines || [];
      } else {
        const jobId = document.getElementById("log-job-select").value;
        if (!jobId) { view.textContent = "Select a job to view its output log"; return; }
        const data = await API.get(`/api/logs/job?job_id=${jobId}&tail=${tail}`);
        lines = data.lines || [];
      }
      view.textContent = lines.join("\n") || "(no output)";
      view.scrollTop = view.scrollHeight;
    } catch (e) {
      view.textContent = "error: " + e.message;
    }
  }

  function debounce(fn, ms) {
    let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
  }

  window.Pages = window.Pages || {};
  window.Pages.logs = { render };
})();