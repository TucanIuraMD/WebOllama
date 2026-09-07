/* Running Models page v2 — live monitoring of loaded Ollama models.
 *
 * Data sources (unchanged, no second telemetry mechanism):
 *  - initial render: GET /api/ollama/running (OllamaClient → /api/ps);
 *  - live updates: existing WS snapshot (snap.ollama.running) via onSnapshot;
 *  - actions: existing /api/ollama/models/{name}/stop.
 *
 * Model VRAM is exactly what /api/ps reports (size_vram) — NOT GPU
 * telemetry. CPU/GPU split is derived from the same payload:
 *   size_vram < size  → partially CPU-offloaded (cpu part = size - size_vram)
 *   size_vram ≥ size  → fully GPU-resident
 * Nothing is invented: fields Ollama doesn't report are shown as "—".
 */
(function () {
  let running = [];
  let loadError = "";
  let offline = false;      // Ollama unreachable (WS snapshot ollama.online === false)
  let loaded = false;       // initial fetch completed
  let stopping = new Set(); // models with a stop in flight (double-click guard)
  let renderSeq = 0;        // stale-render guard (refresh + WS fallback can race)

  async function render(el) {
    const seq = ++renderSeq;
    loadError = "";
    offline = false;
    loaded = false;
    el.innerHTML = buildPage();
    wireStatic();
    draw(); // shows the loading state while the fetch is in flight
    try {
      const data = await API.get("/api/ollama/running");
      if (seq !== renderSeq) return; // a newer render superseded this fetch
      running = data.models || [];
      loadError = "";
    } catch (e) {
      if (seq !== renderSeq) return;
      running = [];
      loadError = e.message || "failed to load running models";
    }
    loaded = true;
    draw();
  }

  function buildPage() {
    return `
      <div class="card">
        <div class="card-title">Running Models <span class="text-dim" id="run-count"></span>
          <span class="right"><button class="btn-sm" id="run-refresh" title="Refresh">⟳</button></span>
        </div>
        <div id="run-body"></div>
      </div>`;
  }

  function wireStatic() {
    const refresh = document.getElementById("run-refresh");
    if (refresh) refresh.onclick = () => render(document.getElementById("page-container"));
  }

  /* ---- state rendering: loading / error / offline / empty / cards ---- */
  function stateHtml() {
    if (!loaded) {
      return `<div class="empty"><div class="big">⏳</div>Loading running models…</div>`;
    }
    if (loadError) {
      // API error ≠ empty. Show what failed; retry button stays available.
      return `<div class="empty"><div class="big">⚠️</div>Failed to load running models
                <div class="text-dim">${esc(loadError)}</div></div>`;
    }
    if (offline) {
      return `<div class="empty"><div class="big">🔌</div>Ollama unavailable
                <div class="text-dim">Start Ollama (or check the endpoint) — the page updates automatically.</div></div>`;
    }
    if (!running.length) {
      return `<div class="empty"><div class="big">🌙</div>No models loaded into memory
                <div class="text-dim">Run a model from the <a href="#models">Models</a> page.</div></div>`;
    }
    return "";
  }

  /* Derived from /api/ps only — no GPU telemetry, no invented fields. */
  function splitInfo(m) {
    const size = m.size || 0;
    const vram = m.size_vram || 0;
    if (!size || !vram) return null;
    if (vram < size) {
      const cpu = size - vram;
      const gpuPct = Math.round((vram / size) * 100);
      return { kind: "split", gpuPct, vram, cpu };
    }
    return { kind: "gpu", vram };
  }

  function splitBadge(m) {
    const s = splitInfo(m);
    if (!s) return `<span class="text-faint">—</span>`;
    if (s.kind === "gpu") {
      return `<span class="badge running-badge">100% GPU</span>`;
    }
    return `<span class="badge cap" title="Partially CPU-offloaded: GPU ${fmtBytes(s.vram)} · CPU ${fmtBytes(s.cpu)}">${s.gpuPct}% GPU / ${100 - s.gpuPct}% CPU</span>`;
  }

  function cardHtml(m) {
    const d = m.details || {};
    const isStopping = stopping.has(m.name);
    const ctx = m.context_length ? fmtNum(m.context_length) : "—";
    const until = m.expires_at ? String(m.expires_at).slice(0, 19).replace("T", " ") : "—";
    return `
      <div class="run-card" data-model="${esc(m.name)}">
        <div class="run-card-head">
          <span class="mono run-card-name" title="${esc(m.name)}">${esc(m.name)}</span>
          <span class="badge running-badge">● running</span>
        </div>
        <div class="detail-grid run-card-grid">
          <div class="k">Model VRAM</div><div class="v mono" style="color:var(--accent)">${fmtBytes(m.size_vram)}</div>
          <div class="k">Model size</div><div class="v mono">${fmtBytes(m.size)}</div>
          <div class="k">CPU / GPU</div><div class="v">${splitBadge(m)}</div>
          <div class="k">Params</div><div class="v">${esc(d.parameter_size || "—")}</div>
          <div class="k">Quant</div><div class="v">${esc(d.quantization_level || "—")}</div>
          <div class="k">Context</div><div class="v mono">${esc(ctx)}</div>
          <div class="k">Unloads at</div><div class="v mono text-dim">${esc(until)}</div>
        </div>
        <div class="run-card-actions">
          <button class="btn-sm" data-action="run-chat" data-model="${esc(m.name)}">💬 Chat</button>
          <button class="btn-sm btn-danger" data-action="run-stop" data-model="${esc(m.name)}" ${isStopping ? "disabled" : ""}>${isStopping ? "⏳ stopping…" : "⏹ Stop"}</button>
        </div>
      </div>`;
  }

  function draw() {
    const count = document.getElementById("run-count");
    const body = document.getElementById("run-body");
    if (!count || !body) return;
    count.textContent = loaded ? String(running.length) : "";
    const state = stateHtml();
    if (state) { body.innerHTML = state; return; }
    const totalVram = running.reduce((s, m) => s + (m.size_vram || 0), 0);
    body.innerHTML = `
      <div class="run-grid">${running.map(cardHtml).join("")}</div>
      <div class="text-faint" style="margin-top:8px;font-size:12px">
        Total model VRAM: <span class="mono">${fmtBytes(totalVram)}</span>
        <span class="text-dim">(sum of model size_vram from /api/ps — not GPU telemetry)</span>
      </div>`;
  }

  /* ---- live updates: existing WS snapshot, no new polling ---- */
  function onSnapshot(snap) {
    if (App.state.currentPage !== "running") return;
    const ollama = snap.ollama || {};
    offline = ollama.online === false;
    if (!ollama.running) { draw(); return; } // temporarily unavailable: keep last known data, flip state
    const sig = JSON.stringify(ollama.running.map((m) => [m.name, m.size_vram, m.expires_at, m.context_length]));
    if (sig !== JSON.stringify(running.map((m) => [m.name, m.size_vram, m.expires_at, m.context_length]))) {
      running = ollama.running;
      loadError = "";
      draw();
    }
  }

  window.Actions = window.Actions || {};
  window.Actions["run-stop"] = async (data, btn) => {
    const name = data.model;
    if (stopping.has(name)) return; // double-click guard
    stopping.add(name);
    if (btn) { btn.disabled = true; btn.textContent = "⏳ stopping…"; }
    try {
      await API.del(`/api/ollama/models/${encodeURIComponent(name)}/stop`);
      toast("Unloaded " + name, "success");
      // The WS snapshot removes the card; refresh once in case WS is slow.
      setTimeout(() => {
        if (!stopping.has(name)) return;
        stopping.delete(name);
        if (App.state.currentPage === "running") {
          render(document.getElementById("page-container"));
        }
      }, 800);
    } catch (e) {
      stopping.delete(name);
      toast("Stop failed: " + e.message, "error");
      draw(); // restore button state
    }
  };

  window.Actions["run-chat"] = (data) => {
    location.hash = `#chat?model=${encodeURIComponent(data.model)}`;
  };

  window.Pages = window.Pages || {};
  window.Pages.running = { render, onSnapshot, draw, stateHtml, splitInfo };
})();
