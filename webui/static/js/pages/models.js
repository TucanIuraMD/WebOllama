/* Models page — full model manager: list, search, sort, multi-select, show, copy, delete, pull, create */
(function () {
  let models = [];
  let selected = new Set();
  let sortKey = "name";
  let sortAsc = true;
  let filterText = "";
  let filterFamily = "";

  async function render(el) {
    try {
      const data = await API.get("/api/ollama/models");
      models = data.models || [];
    } catch (e) {
      models = [];
    }
    selected.clear();
    el.innerHTML = buildPage();
    renderTable();
    document.getElementById("model-search").oninput = debounce(() => {
      filterText = document.getElementById("model-search").value.toLowerCase();
      renderTable();
    }, 200);
    document.getElementById("model-family").onchange = () => {
      filterFamily = document.getElementById("model-family").value;
      renderTable();
    };
    document.getElementById("select-all").onchange = () => {
      const checked = document.getElementById("select-all").checked;
      filteredModels().forEach((m) => { if (checked) selected.add(m.name); else selected.delete(m.name); });
      renderTable();
      updateBatchBar();
    };
    document.getElementById("batch-delete-btn").onclick = () => batchDelete();
    document.getElementById("pull-btn").onclick = () => showPullModal();
    document.getElementById("create-btn").onclick = () => showCreateModal();
    // populate family filter
    const families = [...new Set(models.map((m) => m.details && m.details.family).filter(Boolean))].sort();
    const sel = document.getElementById("model-family");
    families.forEach((f) => { sel.innerHTML += `<option value="${esc(f)}">${esc(f)}</option>`; });
  }

  function buildPage() {
    return `
      <div class="card">
        <div class="card-title">Models <span class="text-dim" id="model-count">${models.length}</span></div>
        <div class="filter-bar">
          <input type="text" id="model-search" placeholder="Search model name…" />
          <select id="model-family" style="max-width:160px"><option value="">All families</option></select>
          <button class="btn btn-primary btn-sm" id="pull-btn">⬇ Pull</button>
          <button class="btn btn-sm" id="create-btn">➕ Create</button>
          <span style="flex:1"></span>
          <span id="batch-bar" style="display:none">
            <span class="text-dim" id="selected-count">0 selected</span>
            <button class="btn btn-danger btn-sm" id="batch-delete-btn">🗑 Delete</button>
          </span>
        </div>
        <div style="overflow-x:auto">
          <table>
            <thead>
              <tr>
                <th style="width:30px"><input type="checkbox" id="select-all" /></th>
                <th onclick="sortModels('name')" class="nowrap">Name <span class="sort-icon" id="sort-name">▼</span></th>
                <th onclick="sortModels('size')" class="nowrap num">Size <span class="sort-icon" id="sort-size">▽</span></th>
                <th onclick="sortModels('params')" class="nowrap">Parameters</th>
                <th onclick="sortModels('quant')" class="nowrap">Quant</th>
                <th onclick="sortModels('family')" class="nowrap">Family</th>
                <th onclick="sortModels('modified')" class="nowrap">Modified</th>
                <th class="nowrap">Actions</th>
              </tr>
            </thead>
            <tbody id="model-tbody"></tbody>
          </table>
        </div>
      </div>
      <div class="empty" id="model-empty" style="display:none">
        <div class="big">📦</div>
        No models found. Pull one from the library.
      </div>`;
  }

  function filteredModels() {
    return models.filter((m) => {
      if (filterText && !m.name.toLowerCase().includes(filterText)) return false;
      if (filterFamily && (m.details && m.details.family) !== filterFamily) return false;
      return true;
    }).sort((a, b) => {
      let va, vb;
      if (sortKey === "name") { va = a.name.toLowerCase(); vb = b.name.toLowerCase(); }
      else if (sortKey === "size") { va = a.size || 0; vb = b.size || 0; }
      else if (sortKey === "params") { va = (a.details && a.details.parameter_size) || ""; vb = (b.details && b.details.parameter_size) || ""; }
      else if (sortKey === "quant") { va = (a.details && a.details.quantization_level) || ""; vb = (b.details && b.details.quantization_level) || ""; }
      else if (sortKey === "family") { va = (a.details && a.details.family) || ""; vb = (b.details && b.details.family) || ""; }
      else if (sortKey === "modified") { va = a.modified_at || ""; vb = b.modified_at || ""; }
      else { va = a.name; vb = b.name; }
      if (typeof va === "string") return sortAsc ? va.localeCompare(vb) : vb.localeCompare(va);
      return sortAsc ? va - vb : vb - va;
    });
  }

  function renderTable() {
    const tbody = document.getElementById("model-tbody");
    const empty = document.getElementById("model-empty");
    const rows = filteredModels();
    if (!rows.length) { tbody.innerHTML = ""; empty.style.display = "block"; return; }
    empty.style.display = "none";
    document.getElementById("model-count").textContent = rows.length;
    tbody.innerHTML = rows.map((m) => {
      const d = m.details || {};
      const sel = selected.has(m.name) ? "checked" : "";
      return `<tr>
        <td><input type="checkbox" ${sel} onchange="toggleSelect('${esc(m.name)}')" /></td>
        <td class="mono ellipsis" title="${esc(m.name)}">${esc(m.name)}</td>
        <td class="num mono">${fmtBytes(m.size)}</td>
        <td class="text-dim">${esc(d.parameter_size || "—")}</td>
        <td><span class="badge quant">${esc(d.quantization_level || "—")}</span></td>
        <td><span class="badge family">${esc(d.family || "—")}</span></td>
        <td class="text-dim">${(m.modified_at || "").slice(0, 10)}</td>
        <td class="nowrap">
          <button class="btn-sm" data-action="model-show" data-model="${esc(m.name)}">🔍</button>
          <button class="btn-sm" data-action="model-copy" data-model="${esc(m.name)}">📋</button>
          <button class="btn-sm btn-danger" data-action="model-delete" data-model="${esc(m.name)}">🗑</button>
        </td>
      </tr>`;
    }).join("");
    updateBatchBar();
  }

  function updateBatchBar() {
    const bar = document.getElementById("batch-bar");
    const count = document.getElementById("selected-count");
    const n = selected.size;
    bar.style.display = n ? "" : "none";
    if (count) count.textContent = n + " selected";
  }

  window.toggleSelect = function (name) {
    if (selected.has(name)) selected.delete(name); else selected.add(name);
    renderTable();
  };

  window.sortModels = function (key) {
    if (sortKey === key) sortAsc = !sortAsc;
    else { sortKey = key; sortAsc = true; }
    document.querySelectorAll(".sort-icon").forEach((el) => { el.textContent = "▽"; });
    const icon = document.getElementById("sort-" + key);
    if (icon) icon.textContent = sortAsc ? "▼" : "▲";
    renderTable();
  };

  // ---- Actions ----
  function setupActions() {
    window.Actions = window.Actions || {};
    window.Actions["model-show"] = (data) => showModel(data.model);
    window.Actions["model-copy"] = (data) => showCopyModal(data.model);
    window.Actions["model-delete"] = (data) => confirmDelete(data.model);
  }
  setupActions();

  async function showModel(name) {
    try {
      const data = await API.post(`/api/ollama/models/${encodeURIComponent(name)}/show`);
      const d = data.details || {};
      let html = `<div class="detail-grid">`;
      const fields = [
        ["Model", data.model || name],
        ["Family", d.family],
        ["Families", (d.families || []).join(", ")],
        ["Parameter Size", d.parameter_size],
        ["Quantization", d.quantization_level],
        ["Format", d.format],
        ["Parent Model", d.parent_model || "—"],
        ["Capabilities", (data.capabilities || []).join(", ")],
        ["Context Length", d.context_length],
        ["Embedding Length", d.embedding_length],
        ["Digest", data.digest || ""],
      ];
      fields.forEach(([k, v]) => { html += `<div class="k">${k}</div><div class="v mono">${esc(v != null ? String(v) : "—")}</div>`; });
      html += `</div>`;
      if (data.license) html += `<div class="card-title" style="margin-top:14px">License</div><pre style="background:#05070b;padding:10px;border-radius:6px;font-size:12px;max-height:200px;overflow:auto" class="mono">${esc(data.license)}</pre>`;
      if (data.modelfile) html += `<div class="card-title" style="margin-top:14px">Modelfile</div><pre style="background:#05070b;padding:10px;border-radius:6px;font-size:12px;max-height:300px;overflow:auto" class="mono">${esc(data.modelfile)}</pre>`;
      if (data.template) html += `<div class="card-title" style="margin-top:14px">Template</div><pre style="background:#05070b;padding:10px;border-radius:6px;font-size:12px;max-height:200px;overflow:auto" class="mono">${esc(data.template)}</pre>`;
      if (data.parameters) html += `<div class="card-title" style="margin-top:14px">Parameters</div><pre style="background:#05070b;padding:10px;border-radius:6px;font-size:12px;max-height:200px;overflow:auto" class="mono">${esc(JSON.stringify(data.parameters, null, 2))}</pre>`;
      if (data.messages) html += `<div class="card-title" style="margin-top:14px">Messages</div><pre style="background:#05070b;padding:10px;border-radius:6px;font-size:12px;max-height:200px;overflow:auto" class="mono">${esc(JSON.stringify(data.messages, null, 2))}</pre>`;
      openModal(html, { title: `🔍 ${name}`, width: "900px" });
    } catch (e) {
      toast("Show failed: " + e.message, "error");
    }
  }

  function showCopyModal(name) {
    openModal(`
      <div class="form-row">
        <label>Source</label>
        <input type="text" value="${esc(name)}" readonly />
      </div>
      <div class="form-row">
        <label>Destination</label>
        <input type="text" id="copy-dest" placeholder="${esc(name + "-backup")}" />
      </div>
      <button class="btn btn-primary" onclick="doCopy('${esc(name)}')">Copy</button>
    `, { title: `📋 Copy ${name}` });
  }

  window.doCopy = async function (src) {
    const dest = document.getElementById("copy-dest").value.trim();
    if (!dest) { toast("destination required", "error"); return; }
    try {
      await API.post(`/api/ollama/models/${encodeURIComponent(src)}/copy`, { destination: dest });
      toast("Model copied", "success");
      closeModal();
      render(document.getElementById("page-container"));
    } catch (e) {
      toast("Copy failed: " + e.message, "error");
    }
  };

  function confirmDelete(name) {
    openModal(`
      <div class="alert warn">Are you sure you want to delete <strong>${esc(name)}</strong>?</div>
      <div class="toolbar">
        <button class="btn btn-danger" onclick="doDelete('${esc(name)}')">🗑 Delete</button>
        <button class="btn" onclick="closeModal()">Cancel</button>
      </div>
    `, { title: `🗑 Delete ${name}` });
  }

  window.doDelete = async function (name) {
    try {
      await API.del(`/api/ollama/models/${encodeURIComponent(name)}`);
      toast("Deleted", "success");
      closeModal();
      render(document.getElementById("page-container"));
    } catch (e) {
      toast("Delete failed: " + e.message, "error");
    }
  };

  async function batchDelete() {
    if (selected.size === 0) return;
    openModal(`
      <div class="alert warn">Delete <strong>${selected.size}</strong> models?</div>
      <div class="toolbar">
        <button class="btn btn-danger" onclick="doBatchDelete()">🗑 Delete All</button>
        <button class="btn" onclick="closeModal()">Cancel</button>
      </div>
    `, { title: "Batch Delete" });
  }

  window.doBatchDelete = async function () {
    const names = [...selected];
    try {
      const res = await API.post("/api/ollama/models/batch-delete", { models: names });
      const ok = res.results.filter((r) => r.ok).length;
      toast(`Deleted ${ok}/${names.length} models`, "success");
      closeModal();
      selected.clear();
      render(document.getElementById("page-container"));
    } catch (e) {
      toast("Batch delete failed: " + e.message, "error");
    }
  };

  function showPullModal() {
    openModal(`
      <div class="form-row">
        <label>Model name</label>
        <input type="text" id="pull-name" placeholder="qwen3:8b" />
      </div>
      <button class="btn btn-primary" onclick="doPull()">⬇ Pull</button>
      <div id="pull-progress" style="margin-top:12px;display:none">
        <div class="progress"><div id="pull-bar" style="width:0%"></div></div>
        <div class="progress-bar"><span class="progress-label" id="pull-label">starting…</span></div>
      </div>
    `, { title: "⬇ Pull Model" });
  }

  window.doPull = async function () {
    const name = document.getElementById("pull-name").value.trim();
    if (!name) { toast("model name required", "error"); return; }
    const progress = document.getElementById("pull-progress");
    progress.style.display = "block";
    try {
      const res = await API.post("/api/ollama/models/pull", { name });
      const jobId = res.job_id;
      toast("Pull started as job " + jobId, "info");
      closeModal();
      // redirect to jobs page
      location.hash = "#jobs";
    } catch (e) {
      toast("Pull failed: " + e.message, "error");
    }
  };

  function showCreateModal() {
    openModal(`
      <div class="tabs" id="create-tabs">
        <span class="tab active" data-tab="visual">Visual</span>
        <span class="tab" data-tab="raw">Raw Modelfile</span>
      </div>
      <div id="create-visual">
        <div class="form-row">
          <label>Name</label>
          <input type="text" id="create-name" placeholder="my-model:latest" />
        </div>
        <div class="form-row">
          <label>FROM</label>
          <input type="text" id="create-from" placeholder="qwen3:8b" />
        </div>
        <div class="form-row">
          <label>SYSTEM (optional)</label>
          <textarea id="create-system" rows="4" placeholder="You are a helpful assistant…"></textarea>
        </div>
        <div class="form-row">
          <label>PARAMETER (one per line, e.g. temperature 0.7)</label>
          <textarea id="create-params" rows="3" placeholder="temperature 0.7&#10;num_ctx 8192"></textarea>
        </div>
        <div class="form-row">
          <label>TEMPLATE (optional)</label>
          <textarea id="create-template" rows="3" placeholder="&#123;&#123; .System &#125;&#125;&#10;&#123;&#123; .Prompt &#125;&#125;"></textarea>
        </div>
        <div class="form-row">
          <label>LICENSE (optional)</label>
          <textarea id="create-license" rows="3"></textarea>
        </div>
      </div>
      <div id="create-raw" style="display:none">
        <div class="form-row">
          <label>Modelfile</label>
          <textarea id="create-rawfile" rows="16" style="font-family:var(--mono)">FROM qwen3:8b&#10;PARAMETER temperature 0.7&#10;PARAMETER num_ctx 8192</textarea>
        </div>
      </div>
      <button class="btn btn-primary" onclick="doCreate()">Create Model</button>
      <div id="create-progress" style="margin-top:12px;display:none">
        <div class="progress"><div id="create-bar" style="width:0%"></div></div>
        <div class="progress-bar"><span class="progress-label" id="create-label">creating…</span></div>
      </div>
    `, { title: "Create Model", width: "700px" });

    // tab switching
    document.querySelectorAll("#create-tabs .tab").forEach((t) => {
      t.onclick = () => {
        document.querySelectorAll("#create-tabs .tab").forEach((x) => x.classList.remove("active"));
        t.classList.add("active");
        document.getElementById("create-visual").style.display = t.dataset.tab === "visual" ? "" : "none";
        document.getElementById("create-raw").style.display = t.dataset.tab === "raw" ? "" : "none";
        // sync raw file from visual on switch
        if (t.dataset.tab === "raw") syncRaw();
        if (t.dataset.tab === "visual") syncVisual();
      };
    });
  }

  function syncRaw() {
    const parts = [];
    const from = document.getElementById("create-from").value.trim();
    if (from) parts.push("FROM " + from);
    const sys = document.getElementById("create-system").value.trim();
    if (sys) parts.push("SYSTEM \"\"\"" + sys + "\"\"\"");
    const params = document.getElementById("create-params").value.trim();
    if (params) params.split("\n").forEach((l) => { const t = l.trim(); if (t) parts.push("PARAMETER " + t); });
    const tmpl = document.getElementById("create-template").value.trim();
    if (tmpl) parts.push("TEMPLATE \"\"\"" + tmpl + "\"\"\"");
    const lic = document.getElementById("create-license").value.trim();
    if (lic) parts.push("LICENSE \"\"\"" + lic + "\"\"\"");
    document.getElementById("create-rawfile").value = parts.join("\n");
  }

  function syncVisual() {
    const raw = document.getElementById("create-rawfile").value;
    // parse simple modelfile; not perfect but covers basic cases
    const fromMatch = raw.match(/^FROM\s+(.+)$/m);
    const sysMatch = raw.match(/SYSTEM\s+"""([\s\S]*?)"""/);
    const paramsMatch = [...raw.matchAll(/^PARAMETER\s+(.+)$/gm)];
    const tmplMatch = raw.match(/TEMPLATE\s+"""([\s\S]*?)"""/);
    const licMatch = raw.match(/LICENSE\s+"""([\s\S]*?)"""/);
    if (fromMatch) document.getElementById("create-from").value = fromMatch[1].trim();
    if (sysMatch) document.getElementById("create-system").value = sysMatch[1].trim();
    if (paramsMatch.length) document.getElementById("create-params").value = paramsMatch.map((m) => m[1].trim()).join("\n");
    if (tmplMatch) document.getElementById("create-template").value = tmplMatch[1].trim();
    if (licMatch) document.getElementById("create-license").value = licMatch[1].trim();
  }

  window.doCreate = async function () {
    // detect active tab
    const active = document.querySelector("#create-tabs .tab.active");
    const isRaw = active && active.dataset.tab === "raw";
    const name = document.getElementById("create-name").value.trim();
    if (!name) { toast("model name required", "error"); return; }
    let modelfile;
    if (isRaw) {
      modelfile = document.getElementById("create-rawfile").value;
    } else {
      syncRaw();
      modelfile = document.getElementById("create-rawfile").value;
    }
    if (!modelfile) { toast("modelfile is empty", "error"); return; }
    const progress = document.getElementById("create-progress");
    progress.style.display = "block";
    try {
      const res = await API.post("/api/ollama/models/create", { name, modelfile });
      toast("Create started as job " + res.job_id, "info");
      closeModal();
      location.hash = "#jobs";
    } catch (e) {
      toast("Create failed: " + e.message, "error");
    }
  };

  function debounce(fn, ms) {
    let t;
    return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
  }

  window.Pages = window.Pages || {};
  window.Pages.models = { render };
})();