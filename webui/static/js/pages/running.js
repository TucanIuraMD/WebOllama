/* Running Models page — loaded models with stop/unload and VRAM */
(function () {
  let running = [];
  let lastRender = "";

  async function render(el) {
    try {
      const data = await API.get("/api/ollama/running");
      running = data.models || [];
    } catch (e) {
      running = [];
    }
    el.innerHTML = `
      <div class="card">
        <div class="card-title">Running Models <span class="text-dim" id="run-count"></span>
          <span class="right"><button class="btn-sm" onclick="location.reload()">⟳</button></span>
        </div>
        <div id="run-body"></div>
      </div>`;
    draw();
  }

  function draw() {
    const count = document.getElementById("run-count");
    const body = document.getElementById("run-body");
    if (!count || !body) return;
    count.textContent = running.length;
    if (!running.length) {
      body.innerHTML = '<div class="empty"><div class="big">🌙</div>No models loaded into memory</div>';
      return;
    }
    body.innerHTML = `
      <div style="overflow-x:auto">
        <table>
          <thead><tr>
            <th>Model</th><th class="num">Size</th><th class="num">VRAM</th><th>Params</th><th>Quant</th><th>Family</th><th>Until</th><th>Digest</th><th>Actions</th>
          </tr></thead>
          <tbody>${running.map((m) => {
            const d = m.details || {};
            return `<tr>
              <td class="mono">${esc(m.name)}</td>
              <td class="num mono">${fmtBytes(m.size)}</td>
              <td class="num mono" style="color:var(--accent)">${fmtBytes(m.size_vram)}</td>
              <td class="text-dim">${esc(d.parameter_size || "—")}</td>
              <td><span class="badge quant">${esc(d.quantization_level || "—")}</span></td>
              <td><span class="badge family">${esc(d.family || "—")}</span></td>
              <td class="text-dim nowrap">${(m.expires_at || "").slice(0, 19).replace("T", " ")}</td>
              <td class="mono text-faint ellipsis">${esc(m.digest || "").slice(0, 16)}</td>
              <td class="nowrap">
                <button class="btn-sm" data-action="run-stop" data-model="${esc(m.name)}" title="Unload (keep_alive=0)">⏹ Unload</button>
              </td>
            </tr>`;
          }).join("")}</tbody>
        </table>
      </div>
      <div class="text-faint" style="margin-top:8px;font-size:12px">
        Total VRAM: <span class="mono">${fmtBytes(running.reduce((s, m) => s + (m.size_vram || 0), 0))}</span>
      </div>`;
    lastRender = body.innerHTML;
  }

  function onSnapshot(snap) {
    if (App.state.currentPage !== "running") return;
    const ollama = snap.ollama || {};
    if (!ollama.running) return;
    const sig = JSON.stringify(ollama.running.map((m) => [m.name, m.size_vram, m.expires_at]));
    if (sig !== JSON.stringify(running.map((m) => [m.name, m.size_vram, m.expires_at]))) {
      running = ollama.running;
      draw();
    }
  }

  window.Actions = window.Actions || {};
  window.Actions["run-stop"] = async (data) => {
    try {
      await API.del(`/api/ollama/models/${encodeURIComponent(data.model)}/stop`);
      toast("Unloaded " + data.model, "success");
      setTimeout(() => render(document.getElementById("page-container")), 800);
    } catch (e) {
      toast("Stop failed: " + e.message, "error");
    }
  };

  window.Pages = window.Pages || {};
  window.Pages.running = { render, onSnapshot };
})();