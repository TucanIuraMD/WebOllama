/* API client — REST calls with session cookie / CSRF token handling */
const API = {
  async request(method, url, body) {
    const opts = {
      method,
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
    };
    // CSRF header when using cookie sessions
    const csrf = document.cookie.split("; ").find((c) => c.startsWith("csrf="));
    if (csrf) opts.headers["X-CSRF-Token"] = csrf.split("=")[1];
    if (body !== undefined) opts.body = JSON.stringify(body);
    const resp = await fetch(url, opts);
    let data = null;
    const text = await resp.text();
    if (text) {
      try { data = JSON.parse(text); } catch { data = text; }
    }
    if (!resp.ok) {
      const detail = (data && (data.detail || data.message || data.error)) || text || `HTTP ${resp.status}`;
      const err = new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
      err.status = resp.status;
      err.data = data;
      throw err;
    }
    return data;
  },
  get: (url) => API.request("GET", url),
  post: (url, body) => API.request("POST", url, body || {}),
  put: (url, body) => API.request("PUT", url, body || {}),
  del: (url) => API.request("DELETE", url),
};

/* Byte formatter — DECIMAL units, matching `ollama ls` / `ollama ps` output
 * (Ollama CLI prints model sizes in decimal GB: bytes / 1_000_000_000).
 * Division by 1024 (binary units, "GiB") made WebOllama show 6.2 GB where
 * `ollama ls` showed 6.7 GB for the same model. The number displayed here
 * must be directly comparable with the CLI, so: decimal everywhere.
 * API values are passed through untouched — only display changes. */
function fmtBytes(n, dp) {
  if (n === null || n === undefined || isNaN(n)) return "—";
  n = Number(n);
  const units = ["B", "KB", "MB", "GB", "TB", "PB"];
  let i = 0;
  while (n >= 1000 && i < units.length - 1) { n /= 1000; i++; }
  const d = dp !== undefined ? dp : (i === 0 ? 0 : 1);
  return n.toFixed(d) + " " + units[i];
}

function fmtSpeed(bps) {
  if (!bps) return "0 B/s";
  return fmtBytes(bps, 1) + "/s";
}

function fmtNum(n, dp) {
  if (n === null || n === undefined || isNaN(n)) return "—";
  return Number(n).toLocaleString("en-US", { maximumFractionDigits: dp ?? 1 });
}

function esc(s) {
  if (s === null || s === undefined) return "";
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function fmtDuration(sec) {
  sec = Math.max(0, Math.floor(sec || 0));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  if (h) return `${h}h ${m}m ${s}s`;
  if (m) return `${m}m ${s}s`;
  return `${s}s`;
}

function fmtClock(sec) {
  sec = Math.floor(sec || 0);
  return `${String(Math.floor(sec / 3600)).padStart(2, "0")}:${String(Math.floor((sec % 3600) / 60)).padStart(2, "0")}:${String(sec % 60).padStart(2, "0")}`;
}

function escModelName(name) {
  return name.replace(/[/\\]/g, "_").replace(/[^A-Za-z0-9_.:\-]/g, "");
}

function gpuPctColor(pct) {
  if (pct === null || pct === undefined) return "var(--text-faint)";
  if (pct >= 90) return "var(--red)";
  if (pct >= 60) return "var(--yellow)";
  return "var(--green)";
}
