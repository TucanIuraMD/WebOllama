/* Agents page — practical Models × Agents knowledge base.
   Matrix of real Ollama models (rows) × configured agents (columns).
   Each cell is a human-verified assessment: status, capabilities, note.
   This is NOT a benchmark: nothing is scored or auto-filled here.

   Admins can add new agents/capabilities at runtime (SQLite rows via
   POST /api/agents/agents and /api/agents/capabilities) — no restart,
   no hardcoded lists. */
(function () {
  "use strict";

  // status -> display marker (untested != failed: untested is neutral)
  const STATUS_MARK = { untested: "—", failed: "❌", works: "✓", good: "✓✓" };
  const STATUS_LABEL = {
    untested: "Untested", failed: "Failed", works: "Works", good: "Good",
  };

  // ---- state -------------------------------------------------------------
  let data = null;          // {models, agents, capabilities, assessments, counts, ollama_online}
  let assessIdx = {};       // "model\u0000agent_id" -> assessment
  let filters = { search: "", agent: "", capability: "", status: "" };
  let expandedModels = new Set(); // row-expansion => model view (per-agent detail)

  function isAdmin() {
    return !!(App.state.user && App.state.user.role === "admin");
  }

  function idxKey(model, agentId) { return model + "\u0000" + agentId; }

  function rebuildIndex() {
    assessIdx = {};
    (data.assessments || []).forEach((a) => {
      assessIdx[idxKey(a.model, a.agent_id)] = a;
    });
  }

  // ---- data ----------------------------------------------------------------
  async function load() {
    data = await API.get("/api/agents/matrix");
    rebuildIndex();
  }

  async function render(el) {
    el.innerHTML = '<div class="card" style="padding:16px">Loading agents matrix…</div>';
    try {
      await load();
    } catch (e) {
      el.innerHTML = `<div class="card"><div class="alert error">Failed to load: ${esc(e.message)}</div></div>`;
      return;
    }
    el.innerHTML = buildPage();
    bindToolbar(el);
    drawMatrix();
  }

  function buildPage() {
    return `
      <div class="card agents-head">
        <div>
          <div class="title">Agents <span class="text-dim" style="font-size:13px;font-weight:400">— practical model suitability per environment</span></div>
          <div class="sub" id="agents-sub"></div>
        </div>
        <div class="agents-legend">
          <span><span class="st-untested">—</span> untested</span>
          <span><span class="st-failed">❌</span> failed</span>
          <span><span class="st-works">✓</span> works</span>
          <span><span class="st-good">✓✓</span> good</span>
          <span id="agents-ollama-state"></span>
        </div>
      </div>
      <div class="card agents-toolbar">
        <div class="filter-row" id="agents-admin-row" style="display:none">
          <span class="filter-label">&nbsp;</span>
          <button class="btn btn-sm btn-primary" data-action="agents-add">+ Add Agent</button>
          <button class="btn btn-sm" data-action="agents-add-cap">+ Add Capability</button>
        </div>
        <div class="filter-row">
          <span class="filter-label">Search</span>
          <input type="text" id="agents-search" placeholder="Search model name…" style="flex:1;min-width:180px" value="${esc(filters.search)}" />
        </div>
        <div class="filter-row" id="agents-agent-row"><span class="filter-label">Agent</span></div>
        <div class="filter-row" id="agents-cap-row"><span class="filter-label">Capability</span></div>
        <div class="filter-row" id="agents-status-row"><span class="filter-label">Status</span></div>
      </div>
      <div class="agents-matrix-wrap" id="agents-matrix-wrap"></div>`;
  }

  function chip(row, value, label, group, cls) {
    const active = filters[group] === value ? " active" : "";
    return `<span class="filter-chip${cls ? " " + cls : ""}${active}" data-action="agents-filter" data-group="${group}" data-value="${esc(value)}">${label}</span>`;
  }

  function drawFilters() {
    const agentRow = document.getElementById("agents-agent-row");
    agentRow.innerHTML =
      '<span class="filter-label">Agent</span>' +
      chip("agents-agent-row", "", "All", "agent") +
      (data.agents || [])
        .map((a) => chip("agents-agent-row", a.slug, esc(a.name), "agent", a.enabled ? "" : "dim"))
        .join("");
    const capRow = document.getElementById("agents-cap-row");
    capRow.innerHTML =
      '<span class="filter-label">Capability</span>' +
      chip("agents-cap-row", "", "All", "capability") +
      (data.capabilities || [])
        .map((c) => chip("agents-cap-row", c.slug, esc(c.name), "capability"))
        .join("");
    const stRow = document.getElementById("agents-status-row");
    stRow.innerHTML =
      '<span class="filter-label">Status</span>' +
      chip("agents-status-row", "", "All", "status") +
      chip("agents-status-row", "tested", "Tested", "status") +
      chip("agents-status-row", "untested", "Untested", "status") +
      chip("agents-status-row", "works", "Works", "status") +
      chip("agents-status-row", "failed", "Failed", "status") +
      chip("agents-status-row", "good", "Good", "status");
  }

  function bindToolbar(el) {
    const search = document.getElementById("agents-search");
    let t;
    search.oninput = () => {
      clearTimeout(t);
      t = setTimeout(() => {
        filters.search = search.value.toLowerCase();
        drawMatrix();
      }, 200);
    };
    const st = document.getElementById("agents-ollama-state");
    if (st) {
      st.innerHTML = data.ollama_online
        ? '<span style="color:var(--green)">● Ollama online</span>'
        : '<span style="color:var(--red)">● Ollama offline — list may be stale</span>';
    }
    // admin-only config controls
    const adminRow = document.getElementById("agents-admin-row");
    if (adminRow) adminRow.style.display = isAdmin() ? "flex" : "none";
    // NOTE: drawFilters() must run BEFORE any chip binding — filter chips use
    // the global data-action delegation (window.Actions.agents-filter), so
    // they stay clickable no matter how often they are re-rendered.
    drawFilters();
  }

  // ---- filtering ------------------------------------------------------------
  function visibleAssessmentsFor(modelName, agentId) {
    const a = assessIdx[idxKey(modelName, agentId)];
    if (!a) return { match: filters.status === "" || filters.status === "untested", assessment: null };
    if (filters.capability) {
      const caps = (a.capabilities || []).map((c) => c.slug);
      if (!caps.includes(filters.capability)) return { match: false, assessment: a };
    }
    const st = a.status;
    if (filters.status === "tested" && st === "untested") return { match: false, assessment: a };
    if (filters.status === "untested" && st !== "untested") return { match: false, assessment: a };
    if (["works", "failed", "good"].includes(filters.status) && st !== filters.status) {
      return { match: false, assessment: a };
    }
    return { match: true, assessment: a };
  }

  function modelVisible(m) {
    const name = m.name.toLowerCase();
    if (filters.search && !name.includes(filters.search)) return false;
    if (filters.agent && !m._agentCols.some((col) => col.visible)) return false;
    if (filters.capability) {
      const hit = m._agentCols.some((col) => col.visible && col.assessment &&
        (col.assessment.capabilities || []).some((c) => c.slug === filters.capability));
      if (!hit) return false;
    }
    if (filters.status) {
      const hit = m._agentCols.some((col) => col.visible);
      if (!hit) return false;
    }
    return true;
  }

  function computeRows() {
    const agents = data.agents || [];
    const agentFilter = filters.agent ? agents.find((a) => a.slug === filters.agent) : null;
    const shownAgents = agentFilter ? [agentFilter] : agents;
    return (data.models || [])
      .map((m) => {
        m._agentCols = shownAgents.map((ag) => {
          const r = visibleAssessmentsFor(m.name, ag.id);
          return { agent: ag, visible: r.match, assessment: r.assessment };
        });
        return m;
      })
      .filter(modelVisible);
  }

  // ---- matrix -----------------------------------------------------------------
  function drawMatrix() {
    const wrap = document.getElementById("agents-matrix-wrap");
    if (!wrap) return;
    const agents = data.agents || [];
    const shownAgents = filters.agent ? agents.filter((a) => a.slug === filters.agent) : agents;
    const rows = computeRows();

    const sub = document.getElementById("agents-sub");
    if (sub) {
      const tested = (data.assessments || []).filter((a) => a.status !== "untested").length;
      sub.textContent = `${rows.length} of ${data.counts.models} models × ${shownAgents.length} agents · ` +
        `${data.counts.assessments} assessments (${tested} tested)`;
    }

    if (!rows.length) {
      wrap.innerHTML = `<div class="empty" style="padding:40px"><div class="big">🤖</div>${data.counts.models
        ? "No models match the current filters."
        : "No models: Ollama is unreachable."}</div>`;
      return;
    }

    const head = `<tr>
      <th class="model-col">Model</th>
      ${shownAgents.map((a) => `<th class="agent-col${a.enabled ? "" : " dim"}" title="${esc(a.description || "")}">${esc(a.name)}</th>`).join("")}
    </tr>`;

    const body = rows.map((m) => {
      const open = expandedModels.has(m.name);
      const cells = shownAgents.map((ag) => {
        const col = m._agentCols.find((c) => c.agent.id === ag.id);
        if (!col.visible) return '<td class="agent-cell"></td>';
        const a = col.assessment;
        const st = a ? a.status : "untested";
        const caps = a && a.capabilities && a.capabilities.length
          ? a.capabilities.map((c) => c.slug).join(", ") : "";
        const title = `${m.name} × ${ag.name}\nstatus: ${STATUS_LABEL[st]}${a && a.tested_at ? "\ntested: " + new Date(a.tested_at * 1000).toLocaleString() : ""}${a && a.note ? "\nnote: " + a.note : ""}`;
        return `<td class="agent-cell"><button class="cell-btn st-${st}" data-action="agents-cell" data-model="${esc(m.name)}" data-agent="${ag.id}" title="${esc(title)}">` +
          `${STATUS_MARK[st]}${caps ? `<span class="cell-cap">${esc(caps)}</span>` : ""}</button></td>`;
      }).join("");
      const meta = [m.parameter_size, m.quantization_level].filter(Boolean).join(" · ");
      return `<tr data-model-row="${esc(m.name)}">
        <td class="model-col">
          <a href="javascript:void(0)" data-action="agents-model" data-model="${esc(m.name)}" class="model-name" title="${esc(m.name)}">${esc(m.name)}</a>
          <span class="model-meta">${esc(meta || "")}</span>
          ${open ? modelView(m) : ""}
        </td>
        ${cells}
      </tr>`;
    }).join("");

    wrap.innerHTML = `<table class="agents-matrix"><thead>${head}</thead><tbody>${body}</tbody></table>`;
  }

  // ---- model view (row expansion) -----------------------------------------------
  function modelView(m) {
    const rows = (data.agents || []).map((ag) => {
      const a = assessIdx[idxKey(m.name, ag.id)];
      const st = a ? a.status : "untested";
      const caps = a && a.capabilities && a.capabilities.length
        ? a.capabilities.map((c) => `<span class="badge">${esc(c.name)}</span>`).join(" ")
        : "";
      const tested = a && a.tested_at ? new Date(a.tested_at * 1000).toLocaleString() : "";
      // Whole row opens the assessment editor (same editor as matrix cells);
      // the explicit Edit button is the accessible fallback.
      return `<div class="mv-row" data-action="agents-cell" data-model="${esc(m.name)}" data-agent="${ag.id}" title="Click to edit assessment for ${esc(ag.name)}">
        <span class="mv-agent">${esc(ag.name)}</span>
        <span class="mv-status st-${st}">${STATUS_MARK[st]}</span>
        <span class="mv-caps">${caps || '<span class="text-faint">—</span>'}</span>
        <span class="mv-note">${esc(a && a.note ? a.note : "")}${tested ? ` <span class="text-faint">· ${esc(tested)}</span>` : ""}</span>
        <button type="button" class="btn btn-sm mv-edit" data-action="agents-cell" data-model="${esc(m.name)}" data-agent="${ag.id}">Edit</button>
      </div>`;
    }).join("");
    return `<div class="card" style="margin:8px 0 4px;background:var(--bg-3)">
      <div class="card-title" style="font-size:13px">Assessments — ${esc(m.name)}
        <span class="right"><button type="button" class="btn btn-sm" data-action="agents-model" data-model="${esc(m.name)}">Collapse</button></span>
      </div>
      ${rows}
    </div>`;
  }

  // ---- cell editor ------------------------------------------------------------
  function openCellEditor(modelName, agentId) {
    const agent = (data.agents || []).find((a) => a.id === Number(agentId));
    if (!agent) return;
    const a = assessIdx[idxKey(modelName, agent.id)] || null;
    const st = a ? a.status : "untested";
    const caps = new Set((a && a.capabilities || []).map((c) => c.id));
    const capBoxes = (data.capabilities || []).map((c) =>
      `<label><input type="checkbox" value="${c.id}" ${caps.has(c.id) ? "checked" : ""}/> ${esc(c.name)}</label>`
    ).join("");
    const statusRadios = Object.keys(STATUS_MARK).map((s) =>
      `<label class="filter-chip${st === s ? " active" : ""}" style="cursor:pointer">
         <input type="radio" name="ed-status" value="${s}" ${st === s ? "checked" : ""} style="accent-color:var(--accent)"/> ${STATUS_MARK[s]} ${STATUS_LABEL[s]}
       </label>`
    ).join("");
    openModal(`
      <div class="form-row"><label>Model</label><input type="text" value="${esc(modelName)}" readonly class="mono" /></div>
      <div class="form-row"><label>Agent</label><input type="text" value="${esc(agent.name)}" readonly /></div>
      <div class="form-row"><label>Status</label>
        <div class="ed-status-row">${statusRadios}</div>
      </div>
      <div class="form-row"><label>Capabilities</label><div class="ed-caps">${capBoxes}</div></div>
      <div class="form-row"><label>Note</label><textarea id="ed-note" rows="3" placeholder="Что проверяли, как работает…">${esc(a && a.note ? a.note : "")}</textarea></div>
      <div class="form-row"><label>Tested at</label>
        <input type="text" readonly value="${a && a.tested_at ? new Date(a.tested_at * 1000).toLocaleString() : "will be set automatically on save"}" />
      </div>
      <div id="ed-error" class="alert error" style="display:none"></div>
      <div class="toolbar">
        ${a ? '<button class="btn btn-danger btn-sm" onclick="AgentsPage.resetCell()">↺ Reset to untested</button>' : ""}
        <span style="flex:1"></span>
        <button class="btn" onclick="closeModal()">Cancel</button>
        <button class="btn btn-primary" onclick="AgentsPage.saveCell()">Save</button>
      </div>
    `, { title: `${esc(modelName)} × ${esc(agent.name)}`, width: "620px" });

    AgentsPage._editing = { model: modelName, agentId: agent.id, savedAt: a && a.tested_at };
    // radio chips highlight
    document.querySelectorAll('input[name="ed-status"]').forEach((r) => {
      r.onchange = () => {
        document.querySelectorAll("#modal .ed-status-row .filter-chip").forEach((c) => c.classList.remove("active"));
        r.closest(".filter-chip").classList.add("active");
      };
    });
  }

  async function saveCell() {
    const { model, agentId } = AgentsPage._editing;
    const status = document.querySelector('input[name="ed-status"]:checked').value;
    const note = document.getElementById("ed-note").value.trim();
    const capabilities = [...document.querySelectorAll("#modal .ed-caps input:checked")].map((i) => Number(i.value));
    try {
      const res = await API.post("/api/agents/assessments", {
        model, agent_id: agentId, status, note, capabilities,
      });
      // update local state without a full page reload
      const a = res.assessment;
      data.assessments = (data.assessments || []).filter(
        (x) => !(x.model === a.model && x.agent_id === a.agent_id)
      );
      data.assessments.push(a);
      data.counts.assessments = data.assessments.length;
      rebuildIndex();
      closeModal();
      drawMatrix();
      toast("Assessment saved", "success");
    } catch (e) {
      showFormError("ed-error", e);
    }
  }

  async function resetCell() {
    const { model, agentId } = AgentsPage._editing;
    const a = assessIdx[idxKey(model, agentId)];
    if (!a) { closeModal(); return; }
    try {
      await API.del(`/api/agents/assessments/${a.id}`);
      data.assessments = data.assessments.filter((x) => x.id !== a.id);
      data.counts.assessments = data.assessments.length;
      rebuildIndex();
      closeModal();
      drawMatrix();
      toast("Assessment reset to untested", "success");
    } catch (e) {
      showFormError("ed-error", e);
    }
  }

  // ---- admin config: add agent / capability -------------------------------------
  function slugify(name) {
    // mirrors the server-side _slugify fallback
    return name.trim().toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
  }

  function showFormError(id, e) {
    const el = document.getElementById(id);
    if (el) {
      el.textContent = e.message || String(e);
      el.style.display = "block";
    }
    toast((e.message || String(e)), "error");
  }

  function openAgentForm() {
    openModal(`
      <div class="form-row"><label>Name *</label><input type="text" id="af-name" placeholder="e.g. Automation" autofocus /></div>
      <div class="form-row"><label>Slug</label><input type="text" id="af-slug" placeholder="auto from name" /><div class="text-faint" style="font-size:11px;margin-top:2px">optional — generated from the name if left empty</div></div>
      <div class="form-row"><label>Description</label><input type="text" id="af-desc" placeholder="what this environment is for" /></div>
      <div class="form-row"><label>Enabled</label>
        <label style="display:flex;gap:6px;align-items:center;font-size:13px"><input type="checkbox" id="af-enabled" checked /> show as a matrix column</label>
      </div>
      <div id="af-error" class="alert error" style="display:none"></div>
      <div class="toolbar">
        <span style="flex:1"></span>
        <button class="btn" onclick="closeModal()">Cancel</button>
        <button class="btn btn-primary" onclick="AgentsPage.submitAgent()">Save</button>
      </div>
    `, { title: "🤖 Add Agent", width: "480px" });

    const nameEl = document.getElementById("af-name");
    const slugEl = document.getElementById("af-slug");
    let slugTouched = false;
    slugEl.oninput = () => { slugTouched = slugEl.value.trim() !== ""; };
    nameEl.oninput = () => {
      if (!slugTouched) slugEl.value = slugify(nameEl.value);
    };
    nameEl.focus();
  }

  async function submitAgent() {
    const name = document.getElementById("af-name").value.trim();
    const payload = {
      name,
      slug: document.getElementById("af-slug").value.trim(),
      description: document.getElementById("af-desc").value.trim(),
      enabled: document.getElementById("af-enabled").checked,
    };
    if (!name) { showFormError("af-error", { message: "agent name required" }); return; }
    try {
      const res = await API.post("/api/agents/agents", payload);
      data.agents.push(res.agent);
      data.counts.agents = (data.counts.agents || 0) + 1;
      closeModal();
      drawFilters();
      drawMatrix();
      toast(`Agent "${res.agent.name}" added`, "success");
    } catch (e) {
      showFormError("af-error", e);
    }
  }

  function openCapabilityForm() {
    openModal(`
      <div class="form-row"><label>Name *</label><input type="text" id="cf-name" placeholder="e.g. OCR" autofocus /></div>
      <div class="form-row"><label>Slug</label><input type="text" id="cf-slug" placeholder="auto from name" /><div class="text-faint" style="font-size:11px;margin-top:2px">optional — generated from the name if left empty</div></div>
      <div class="form-row"><label>Description</label><input type="text" id="cf-desc" placeholder="what this capability means" /></div>
      <div id="cf-error" class="alert error" style="display:none"></div>
      <div class="toolbar">
        <span style="flex:1"></span>
        <button class="btn" onclick="closeModal()">Cancel</button>
        <button class="btn btn-primary" onclick="AgentsPage.submitCapability()">Save</button>
      </div>
    `, { title: "🧩 Add Capability", width: "480px" });

    const nameEl = document.getElementById("cf-name");
    const slugEl = document.getElementById("cf-slug");
    let slugTouched = false;
    slugEl.oninput = () => { slugTouched = slugEl.value.trim() !== ""; };
    nameEl.oninput = () => {
      if (!slugTouched) slugEl.value = slugify(nameEl.value);
    };
    nameEl.focus();
  }

  async function submitCapability() {
    const name = document.getElementById("cf-name").value.trim();
    const payload = {
      name,
      slug: document.getElementById("cf-slug").value.trim(),
      description: document.getElementById("cf-desc").value.trim(),
    };
    if (!name) { showFormError("cf-error", { message: "capability name required" }); return; }
    try {
      const res = await API.post("/api/agents/capabilities", payload);
      data.capabilities.push(res.capability);
      closeModal();
      drawFilters();
      drawMatrix();
      toast(`Capability "${res.capability.name}" added`, "success");
    } catch (e) {
      showFormError("cf-error", e);
    }
  }

  // ---- actions (data-action hooks used across the page) ------------------------
  window.AgentsPage = {
    saveCell, resetCell, submitAgent, submitCapability,
    _editing: null,
  };

  function setupActions() {
    window.Actions = window.Actions || {};
    window.Actions["agents-cell"] = (ds) => openCellEditor(ds.model, ds.agent);
    window.Actions["agents-model"] = (ds) => {
      const name = ds.model;
      if (expandedModels.has(name)) expandedModels.delete(name);
      else expandedModels.add(name);
      drawMatrix();
    };
    window.Actions["agents-filter"] = (ds) => {
      const g = ds.group, v = ds.value;
      filters[g] = filters[g] === v ? "" : v;
      drawFilters();
      drawMatrix();
    };
    window.Actions["agents-add"] = () => {
      if (!isAdmin()) return;
      openAgentForm();
    };
    window.Actions["agents-add-cap"] = () => {
      if (!isAdmin()) return;
      openCapabilityForm();
    };
  }
  setupActions();

  window.Pages = window.Pages || {};
  window.Pages.agents = { render };
})();
