/* WebOllama SPA shell — router, global state, modal, status indicators */
const App = {
  state: {
    snapshot: null,
    user: null,
    authEnabled: true,
    currentPage: "dashboard",
    ollamaOnline: null,
  },

  init() {
    this.bindEvents();
    this.checkAuth();
    window.addEventListener("hashchange", () => this.route());
  },

  bindEvents() {
    window.login = login;
    window.logout = logout;
    window.closeModal = closeModal;
    window.openModal = openModal;
    window.toast = toast;
    document.addEventListener("click", (e) => {
      const m = e.target.closest("[data-action]");
      if (m && !m.disabled) {
        const action = m.dataset.action;
        const handler = window.Actions && window.Actions[action];
        if (handler) handler(m.dataset, m);
      }
    });
  },

  async checkAuth() {
    try {
      const enabled = await API.get("/api/auth/enabled");
      this.state.authEnabled = enabled.auth_enabled;
    } catch { this.state.authEnabled = true; }

    if (!this.state.authEnabled) {
      this.state.user = { username: "local", role: "admin" };
      this.showApp();
      return;
    }
    try {
      const me = await API.get("/api/auth/me");
      this.state.user = me;
      this.showApp();
    } catch {
      this.showLogin();
    }
  },

  showLogin() {
    document.getElementById("app").style.display = "none";
    document.getElementById("login-overlay").style.display = "flex";
  },

  showApp() {
    document.getElementById("login-overlay").style.display = "none";
    document.getElementById("app").style.display = "block";
    const userEl = document.getElementById("user-display");
    userEl.textContent = "👤 " + this.state.user.username;
    if (this.state.user.role === "admin") {
      // full nav
      document.querySelectorAll(".nav-item").forEach((n) => (n.style.display = "block"));
    }
    this.route();
  },

  route() {
    const hash = (location.hash || "#dashboard").slice(1);
    const qIndex = hash.indexOf("?");
    const page = (qIndex === -1 ? hash : hash.slice(0, qIndex));
    // per-page query params, e.g. #chat?model=qwen3:8b — pages read them
    // via App.state.pageQuery (kept until the next route change).
    this.state.pageQuery = {};
    if (qIndex !== -1) {
      try {
        for (const [k, v] of new URLSearchParams(hash.slice(qIndex + 1))) {
          this.state.pageQuery[k] = v;
        }
      } catch { /* malformed query — ignore */ }
    }
    this.state.currentPage = page;
    document.querySelectorAll(".nav-item").forEach((a) => {
      a.classList.toggle("active", a.dataset.page === page);
    });
    const loader = document.getElementById("page-loader");
    const container = document.getElementById("page-container");
    loader.style.display = "block";
    container.innerHTML = "";
    const renderer = window.Pages && window.Pages[page];
    if (!renderer) {
      container.innerHTML = '<div class="empty"><div class="big">404</div>Unknown page</div>';
      loader.style.display = "none";
      return;
    }
    // let the page render (sync or async); loader hides when render completes
    Promise.resolve()
      .then(() => renderer.render(container))
      .catch((err) => {
        console.error("page error:", err);
        container.innerHTML = `<div class="empty"><div class="big">⚠</div>${esc(err.message || "page failed to render")}</div>`;
      })
      .finally(() => { loader.style.display = "none"; });
  },

  updateFromSnapshot(snap) {
    // Stale-snapshot guard: never apply a snapshot older than the one we
    // already rendered (WS reconnects / racing GETs can reorder delivery).
    const ts = Number(snap && snap.ts);
    if (Number.isFinite(ts) && Number.isFinite(this._lastSnapshotTs) && ts < this._lastSnapshotTs) {
      console.warn("Ignoring stale snapshot:", ts, "<", this._lastSnapshotTs);
      return;
    }
    if (Number.isFinite(ts)) this._lastSnapshotTs = ts;
    this.state.snapshot = snap;
    // top bar ollama indicator
    const ollama = snap.ollama || {};
    const online = !!ollama.online;
    this.state.ollamaOnline = online;
    const el = document.getElementById("ollama-indicator");
    const txt = document.getElementById("ollama-status-text");
    if (online) {
      el.style.color = "var(--green)";
      txt.textContent = `ONLINE ${ollama.version || ""}`;
    } else {
      el.style.color = "var(--red)";
      txt.textContent = "OLLAMA OFFLINE";
    }
    // gpu indicator
    const gpuEl = document.getElementById("gpu-indicator");
    const gpu = snap.gpu || {};
    if (gpu.available && gpu.gpus && gpu.gpus.length) {
      const g = gpu.gpus[0];
      gpuEl.style.display = "";
      gpuEl.textContent = `${g.name} ${g.utilization ?? "—"}% · ${g.temperature ?? "—"}°C`;
      gpuEl.style.color = gpuPctColor(g.utilization);
    } else {
      gpuEl.style.display = "none";
    }
    // notify current page of live updates
    const renderer = window.Pages && window.Pages[this.state.currentPage];
    if (renderer && renderer.onSnapshot) {
      try { renderer.onSnapshot(snap, this.state.currentPage); } catch (e) { console.error(e); }
    }
  },
};

// ---- Modal helpers ----
function openModal(html, { title = "", width = "800px" } = {}) {
  const overlay = document.getElementById("modal-overlay");
  const modal = document.getElementById("modal");
  const body = document.getElementById("modal-body");
  body.innerHTML = `
    <div class="modal-head">
      <h3>${esc(title)}</h3>
      <button class="btn-ghost" onclick="closeModal()">✕</button>
    </div>
    <div class="modal-body">${html}</div>`;
  modal.style.width = width;
  overlay.style.display = "block";
  modal.style.display = "block";
}

function closeModal() {
  document.getElementById("modal-overlay").style.display = "none";
  document.getElementById("modal").style.display = "none";
}

// ---- Toast ----
function toast(msg, type = "info") {
  const el = document.createElement("div");
  el.className = "alert " + type;
  el.style.position = "fixed";
  el.style.bottom = "20px";
  el.style.right = "20px";
  el.style.zIndex = "300";
  el.style.boxShadow = "0 4px 20px rgba(0,0,0,.4)";
  el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 5000);
}

async function login() {
  const user = document.getElementById("login-user").value.trim();
  const pass = document.getElementById("login-pass").value;
  const errEl = document.getElementById("login-error");
  errEl.style.display = "none";
  try {
    const res = await API.post("/api/auth/login", { username: user, password: pass });
    App.state.user = res.user;
    App.showApp();
  } catch (e) {
    errEl.textContent = e.message;
    errEl.style.display = "block";
  }
}

async function logout() {
  try { await API.post("/api/auth/logout"); } catch {}
  location.hash = "#dashboard";
  location.reload();
}

// ---- start ----
WS.on((msg) => {
  if (msg.type === "snapshot") App.updateFromSnapshot(msg);
  else if (msg.type === "job_update") {
    const renderer = window.Pages && window.Pages[App.state.currentPage];
    if (renderer && renderer.onJobUpdate) {
      try { renderer.onJobUpdate(msg.job); } catch (e) { console.error(e); }
    }
  }
});

App.init();
