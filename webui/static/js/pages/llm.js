/* LLM API page — OpenAI-compatible endpoints (Ollama, OmniRouter, +custom) */
(function () {
  let endpoints = [];

  async function render(el) {
    try {
      const data = await API.get("/api/llm");
      endpoints = data.endpoints || [];
    } catch (e) {
      endpoints = [];
      el.innerHTML = `<div class="card"><div class="alert error">${esc(e.message)}</div></div>`;
      return;
    }
    el.innerHTML = `
      <div class="card" style="padding:14px 16px">
        <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px">
          <div>
            <div style="font-size:18px;font-weight:700">LLM API</div>
            <div class="text-dim" style="font-size:13px;margin-top:2px">OpenAI-compatible endpoints available on this server</div>
          </div>
          <button class="btn btn-primary btn-sm" onclick="LLMPage.addApi()">➕ Add API</button>
        </div>
      </div>
      <div id="llm-cards" class="grid"></div>
    `;
    draw();
  }

  function draw() {
    const cards = document.getElementById("llm-cards");
    if (!cards) return;
    if (!endpoints.length) {
      cards.innerHTML = '<div class="empty"><div class="big">🔌</div>No endpoints configured</div>';
      return;
    }
    cards.innerHTML = endpoints.map((e) => card(e)).join("");
    // bind buttons
    endpoints.forEach((e) => {
      const btnCheck = document.getElementById(`llm-check-${e.id}`);
      const btnModels = document.getElementById(`llm-models-${e.id}`);
      const btnCopy = document.getElementById(`llm-copy-${e.id}`);
      const btnEdit = document.getElementById(`llm-edit-${e.id}`);
      const btnDel = document.getElementById(`llm-del-${e.id}`);
      if (btnCheck) btnCheck.onclick = () => doCheck(e.id);
      if (btnModels) btnModels.onclick = () => showModels(e.id, e.name);
      if (btnCopy) btnCopy.onclick = () => copyUrl(e.base_url);
      if (btnEdit) btnEdit.onclick = () => editApi(e);
      if (btnDel) btnDel.onclick = () => deleteApi(e);
    });
  }

  function card(e) {
    const st = e.status || {};
    const online = st.online;
    const statusHtml = !e.enabled
      ? '<span class="status-pill status-warn">⏸ DISABLED</span>'
      : online === true
        ? '<span class="status-pill status-on">● ONLINE</span>'
        : online === false
          ? '<span class="status-pill status-off">● OFFLINE</span>'
          : '<span class="status-pill status-idle">● checking…</span>';
    const modelsCount = st.models_count != null ? st.models_count : "—";
    const latency = st.latency_ms != null ? st.latency_ms + " ms" : "—";
    const checked = st.checked_at ? new Date(st.checked_at * 1000).toLocaleTimeString() : "—";
    const err = st.error ? `<div class="alert error" style="margin-top:8px">${esc(st.error)}</div>` : "";
    return `
      <div class="card" style="margin:0">
        <div class="card-title" style="margin-bottom:6px">
          <span style="font-size:15px;text-transform:none;color:var(--text);font-weight:600">${esc(e.name)}</span>
          <span class="right">${statusHtml}</span>
        </div>
        <div class="text-faint" style="font-size:12px;margin-bottom:10px">OpenAI Compatible</div>
        <div class="mono" style="background:#05070b;border:1px solid var(--border);border-radius:6px;padding:8px 10px;font-size:12.5px;word-break:break-all;margin-bottom:12px">${esc(e.base_url)}</div>
        <div class="grid grid-3" style="gap:8px;margin-bottom:12px">
          <div class="metric-tile" style="padding:8px 10px"><div class="metric-label">Status</div><div class="metric-value mono" style="font-size:15px">${online === true ? "ONLINE" : online === false ? "OFFLINE" : "—"}</div></div>
          <div class="metric-tile" style="padding:8px 10px"><div class="metric-label">Models</div><div class="metric-value mono" style="font-size:15px">${modelsCount}</div></div>
          <div class="metric-tile" style="padding:8px 10px"><div class="metric-label">Latency</div><div class="metric-value mono" style="font-size:15px">${latency}</div></div>
        </div>
        <div class="text-faint" style="font-size:12px;margin-bottom:12px">
          Last check: <span class="mono">${checked}</span>
          ${e.api_key ? `<span class="badge" style="margin-left:6px">key ${esc(e.api_key)}</span>` : ""}
        </div>
        <div class="toolbar">
          <button class="btn-sm" id="llm-check-${e.id}">🔍 Check</button>
          <button class="btn-sm" id="llm-models-${e.id}">📦 Models</button>
          <button class="btn-sm" id="llm-copy-${e.id}">📋 Copy URL</button>
          ${App.state.user && App.state.user.role === "admin" ? `
            <span style="flex:1"></span>
            <button class="btn-sm" id="llm-edit-${e.id}">✏️</button>
            <button class="btn-sm btn-danger" id="llm-del-${e.id}">🗑</button>` : ""}
        </div>
        ${err}
      </div>`;
  }

  async function doCheck(id) {
    const cardEl = document.querySelector(`#llm-check-${id}`);
    if (cardEl) cardEl.disabled = true;
    try {
      const status = await API.post(`/api/llm/${encodeURIComponent(id)}/check`);
      const e = endpoints.find((x) => x.id === id);
      if (e) { e.status = status; draw(); }
    } catch (e2) {
      toast("Check failed: " + e2.message, "error");
    }
  }

  async function showModels(id, name) {
    openModal(`<div class="empty"><div class="big spin">⟳</div>loading models…</div>`, { title: `📦 ${esc(name)} — Models` });
    try {
      const data = await API.get(`/api/llm/${encodeURIComponent(id)}/models`);
      if (!data.ok) throw new Error(data.error || "request failed");
      const models = data.models || [];
      const extraKeys = collectExtraKeys(models);
      let html = `<div class="text-dim" style="margin-bottom:8px">${models.length} models · latency ${data.latency_ms} ms</div>
        <div style="overflow-x:auto"><table>
        <thead><tr><th>id</th><th>object</th><th>created</th><th>owned_by</th>${extraKeys.map((k) => `<th>${esc(k)}</th>`).join("")}</tr></thead>
        <tbody>${models.map((m) => {
          const created = m.created ? new Date(m.created * 1000).toLocaleDateString() : "—";
          return `<tr>
            <td class="mono ellipsis" style="max-width:260px" title="${esc(m.id)}">${esc(m.id)}</td>
            <td class="text-dim">${esc(m.object || "—")}</td>
            <td class="text-dim">${created}</td>
            <td class="text-dim">${esc(m.owned_by || "—")}</td>
            ${extraKeys.map((k) => `<td class="mono text-dim">${esc(JSON.stringify(m[k]))}</td>`).join("")}
          </tr>`;
        }).join("")}</tbody></table></div>`;
      openModal(html, { title: `📦 ${esc(name)} — Models (${models.length})`, width: "900px" });
    } catch (e2) {
      openModal(`<div class="alert error">${esc(e2.message)}</div>`, { title: `📦 ${esc(name)} — Models` });
    }
  }

  function collectExtraKeys(models) {
    const known = new Set(["id", "object", "created", "owned_by"]);
    const counts = {};
    models.forEach((m) => Object.keys(m).forEach((k) => {
      if (!known.has(k)) counts[k] = (counts[k] || 0) + 1;
    }));
    // only show keys present in at least half the models, cap at 8
    return Object.entries(counts)
      .filter(([, c]) => c >= Math.max(1, models.length / 2))
      .map(([k]) => k)
      .slice(0, 8);
  }

  function copyUrl(url) {
    navigator.clipboard.writeText(url).then(
      () => toast("URL copied", "success"),
      () => toast("Copy failed", "error")
    );
  }

  // ---- Add / Edit / Delete -------------------------------------------------
  function addApi() { editApi(null); }

  function editApi(e) {
    const isEdit = !!e;
    openModal(`
      <div class="form-row"><label>ID (only for new)</label><input type="text" id="llm-eid" value="${isEdit ? esc(e.id) : ""}" ${isEdit ? "readonly" : ""} placeholder="my-endpoint" /></div>
      <div class="form-row"><label>Name</label><input type="text" id="llm-name" value="${isEdit ? esc(e.name) : ""}" placeholder="OpenRouter" /></div>
      <div class="form-row"><label>Base URL</label><input type="text" id="llm-url" value="${isEdit ? esc(e.base_url) : ""}" placeholder="https://example.com/v1" /></div>
      <div class="form-row"><label>API Key (optional)</label><input type="password" id="llm-key" value="" placeholder="${isEdit && e.api_key ? "saved (" + esc(e.api_key) + ") — leave blank to keep" : "sk-..."}" /></div>
      <div class="form-row"><label>Enabled</label><select id="llm-enabled"><option value="true" ${!isEdit || e.enabled ? "selected" : ""}>Yes</option><option value="false" ${isEdit && !e.enabled ? "selected" : ""}>No</option></select></div>
      <div class="toolbar">
        <button class="btn btn-primary" onclick="LLMPage.saveApi(${isEdit ? "'" + esc(e.id) + "'" : "null"})">💾 Save</button>
        <button class="btn" onclick="closeModal()">Cancel</button>
      </div>
    `, { title: isEdit ? `✏️ Edit ${esc(e.name)}` : "➕ Add API" });
  }

  async function saveApi(existingId) {
    const payload = {
      name: document.getElementById("llm-name").value.trim(),
      base_url: document.getElementById("llm-url").value.trim(),
      api_key: document.getElementById("llm-key").value.trim(),
      enabled: document.getElementById("llm-enabled").value === "true",
    };
    if (!existingId) payload.id = document.getElementById("llm-eid").value.trim();
    try {
      if (existingId) {
        await API.put(`/api/llm/${encodeURIComponent(existingId)}`, payload);
        toast("Endpoint updated", "success");
      } else {
        await API.post("/api/llm", payload);
        toast("Endpoint added", "success");
      }
      closeModal();
      render(document.getElementById("page-container"));
    } catch (e) {
      toast(e.message, "error");
    }
  }

  async function deleteApi(e) {
    openModal(`
      <div class="alert warn">Delete endpoint <strong>${esc(e.name)}</strong>?</div>
      <div class="toolbar">
        <button class="btn btn-danger" onclick="LLMPage.doDelete('${esc(e.id)}')">🗑 Delete</button>
        <button class="btn" onclick="closeModal()">Cancel</button>
      </div>`, { title: "Delete endpoint" });
  }

  async function doDelete(id) {
    try {
      await API.del(`/api/llm/${encodeURIComponent(id)}`);
      toast("Endpoint deleted", "success");
      closeModal();
      render(document.getElementById("page-container"));
    } catch (e) {
      toast(e.message, "error");
    }
  }

  // ---- live updates via WebSocket ------------------------------------------
  function onSnapshot(snap) {
    if (App.state.currentPage !== "llm") return;
    if (!snap.llm || !snap.llm.length) return;
    const sig = JSON.stringify(snap.llm.map((e) => [e.id, e.status && e.status.online, e.status && e.status.latency_ms, e.status && e.status.models_count]));
    const curSig = JSON.stringify(endpoints.map((e) => [e.id, e.status && e.status.online, e.status && e.status.latency_ms, e.status && e.status.models_count]));
    if (sig === curSig) return;
    snap.llm.forEach((ne) => {
      const e = endpoints.find((x) => x.id === ne.id);
      if (e) e.status = ne.status;
    });
    draw();
  }

  window.LLMPage = { addApi, saveApi, doDelete };

  window.Pages = window.Pages || {};
  window.Pages.llm = { render, onSnapshot };
})();