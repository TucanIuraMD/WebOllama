/* Settings page — config, users, audit log, password change */
(function () {
  async function render(el) {
    let settings = { overrides: {} };
    try { settings = await API.get("/api/settings"); } catch (e) { console.error(e); }
    let audit = [], users = [];
    try { audit = (await API.get("/api/audit?limit=100")).rows || []; } catch {}
    try { users = (await API.get("/api/auth/users")) || []; } catch {}

    el.innerHTML = `
      <div class="card">
        <div class="card-title">Settings</div>
        <div class="form-grid">
          <div class="form-row">
            <label>Ollama URL</label>
            <input type="url" id="set-ollama-url" value="${esc(settings.ollama_url || "")}" />
          </div>
          <div class="form-row">
            <label>Sysinfo URL (optional)</label>
            <input type="url" id="set-sysinfo-url" value="${esc(settings.sysinfo_url || "")}" />
          </div>
          <div class="form-row">
            <label>Web UI Host</label>
            <input type="text" id="set-webui-host" value="${esc(settings.webui_host || "")}" />
          </div>
          <div class="form-row">
            <label>Port</label>
            <input type="number" id="set-webui-port" value="${settings.webui_port || 8080}" />
          </div>
          <div class="form-row">
            <label>Refresh Interval (s)</label>
            <input type="number" step="0.1" id="set-refresh" value="${settings.refresh_interval || 1}" />
          </div>
          <div class="form-row">
            <label>History Retention (s)</label>
            <input type="number" id="set-retention" value="${settings.history_retention || 3600}" />
          </div>
          <div class="form-row">
            <label>GPU Enabled</label>
            <select id="set-gpu-enabled">
              <option value="true" ${settings.gpu_enabled ? "selected" : ""}>Yes</option>
              <option value="false" ${!settings.gpu_enabled ? "selected" : ""}>No</option>
            </select>
          </div>
          <div class="form-row">
            <label>Auth Enabled</label>
            <input type="text" value="${settings.auth_enabled ? "Yes" : "No"}" readonly />
            <div class="text-faint" style="font-size:11px;margin-top:2px">Change via .env and restart</div>
          </div>
        </div>
        <button class="btn btn-primary" onclick="saveSettings()">💾 Save</button>
        <div id="settings-saved" class="alert success" style="display:none;margin-top:10px">Settings saved (restart required for some)</div>
      </div>

      <div class="card">
        <div class="card-title">V100 Fan Control <span class="text-faint" style="font-size:11px">(v100-fan.service — read-only)</span></div>
        <div id="fan-control-state" class="text-dim">loading…</div>
        <div class="text-faint" style="font-size:11px;margin-top:8px">
          WebOllama only displays the fan state managed by <span class="mono">v100-fan.service</span> (ESP32 controller).
          Source: state file (<span class="mono">V100_FAN_STATE_FILE</span>) or <span class="mono">journalctl -u v100-fan.service</span>.
        </div>
      </div>

      <div class="card">
        <div class="card-title">Change Password</div>
        <div class="form-grid" style="max-width:400px">
          <div class="form-row">
            <label>Current Password</label>
            <input type="password" id="pwd-old" />
          </div>
          <div class="form-row">
            <label>New Password</label>
            <input type="password" id="pwd-new" />
          </div>
        </div>
        <button class="btn" onclick="changePassword()">Change</button>
      </div>

      <div class="card">
        <div class="card-title">Users</div>
        <div id="user-list">${users.length ? `<table><thead><tr><th>ID</th><th>Username</th><th>Role</th><th>Created</th></tr></thead>
          <tbody>${users.map((u) => `<tr><td class="mono">${u.id}</td><td>${esc(u.username)}</td><td><span class="badge">${esc(u.role)}</span></td><td class="text-dim">${u.created_at ? new Date(u.created_at * 1000).toLocaleString() : ""}</td></tr>`).join("")}</tbody></table>` : '<div class="empty text-dim">No users</div>'}</div>
      </div>

      <div class="card">
        <div class="card-title">Audit Log</div>
        <div id="audit-table">${audit.length ? `<div style="overflow-x:auto"><table><thead><tr><th>Time</th><th>User</th><th>Action</th><th>Model</th><th>Result</th><th>Error</th></tr></thead>
          <tbody>${audit.map((a) => `<tr>
            <td class="text-dim nowrap">${new Date((a.ts || 0) * 1000).toLocaleString()}</td>
            <td class="mono">${esc(a.username)}</td>
            <td><span class="badge">${esc(a.action)}</span></td>
            <td class="mono text-dim">${esc(a.model || "—")}</td>
            <td><span class="status-pill ${a.result === "success" ? "status-on" : a.result === "error" ? "status-off" : ""}">${esc(a.result)}</span></td>
            <td class="text-faint ellipsis" style="max-width:200px">${esc(a.error || "")}</td>
          </tr>`).join("")}</tbody></table></div>` : '<div class="empty text-dim">No audit records</div>'}</div>
      </div>`;

    // load V100 fan control state (read-only)
    loadFanState();
  }

  async function loadFanState() {
    const el = document.getElementById("fan-control-state");
    if (!el) return;
    try {
      const gpu = await API.get("/api/gpu");
      const g = (gpu.gpus || [])[0];
      if (!g) {
        el.innerHTML = '<span class="text-faint">no GPU detected</span>';
        return;
      }
      const tgt = g.fan_target, pwm = g.fan_pwm, ok = g.fan_available;
      el.innerHTML = `
        <div class="detail-grid">
          <div class="k">Fan Target</div><div class="v mono">${ok ? (tgt ?? "—") + "%" : "—"}</div>
          <div class="k">Fan PWM</div><div class="v mono">${ok ? (pwm ?? "—") + "%" : "—"}</div>
          <div class="k">Status</div><div class="v">${ok ? '<span class="status-pill status-on">available</span>' : '<span class="status-pill status-off">unavailable</span>'}</div>
          <div class="k">Source</div><div class="v mono">${esc(g.fan_source || "—")}</div>
          ${g.fan_reason ? `<div class="k">Reason</div><div class="v text-faint">${esc(g.fan_reason)}</div>` : ""}
        </div>`;
    } catch (e) {
      el.innerHTML = `<span class="text-faint">${esc(e.message)}</span>`;
    }
  }

  window.saveSettings = async function () {
    const payload = {
      ollama_url: document.getElementById("set-ollama-url").value.trim(),
      sysinfo_url: document.getElementById("set-sysinfo-url").value.trim(),
      webui_host: document.getElementById("set-webui-host").value.trim(),
      webui_port: parseInt(document.getElementById("set-webui-port").value),
      refresh_interval: parseFloat(document.getElementById("set-refresh").value),
      history_retention: parseInt(document.getElementById("set-retention").value),
      gpu_enabled: document.getElementById("set-gpu-enabled").value === "true",
    };
    try {
      await API.put("/api/settings", payload);
      document.getElementById("settings-saved").style.display = "block";
      setTimeout(() => { document.getElementById("settings-saved").style.display = "none"; }, 4000);
    } catch (e) {
      toast("Save failed: " + e.message, "error");
    }
  };

  window.changePassword = async function () {
    const old = document.getElementById("pwd-old").value;
    const pw = document.getElementById("pwd-new").value;
    if (pw.length < 8) { toast("min 8 chars", "error"); return; }
    try {
      await API.post("/api/auth/password", { old_password: old, new_password: pw });
      toast("Password changed", "success");
      document.getElementById("pwd-old").value = "";
      document.getElementById("pwd-new").value = "";
    } catch (e) {
      toast(e.message, "error");
    }
  };

  window.Pages = window.Pages || {};
  window.Pages.settings = { render };
})();