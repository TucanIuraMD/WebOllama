/* Terminal page — Ollama Console with whitelisted commands */
(function () {
  let history = [];
  let historyIdx = -1;

  async function render(el) {
    let allowed;
    try { allowed = await API.get("/api/console/allowed"); } catch { allowed = { commands: [] }; }
    el.innerHTML = `
      <div class="card">
        <div class="card-title">Ollama Console
          <span class="right">
            <button class="btn-sm" id="term-clear">✕ Clear</button>
          </span>
        </div>
        <div style="margin-bottom:10px;color:var(--text-dim);font-size:12px">
          Allowed: ${Object.entries(allowed.commands || {}).map(([k, v]) => `<span class="badge" title="${esc(v)}">${esc(k)}</span>`).join(" ")}
        </div>
        <div class="terminal" id="term-output">WebOllama Console — type 'ollama help' to start</div>
        <div class="terminal-input">
          <span class="prompt">$</span>
          <input type="text" id="term-input" autocomplete="off" placeholder="ollama list" />
        </div>
      </div>`;
    const input = document.getElementById("term-input");
    input.onkeydown = (e) => {
      if (e.key === "Enter") runCommand(input.value);
      else if (e.key === "ArrowUp") {
        e.preventDefault();
        if (history.length) {
          historyIdx = Math.max(0, (historyIdx < 0 ? history.length - 1 : historyIdx - 1));
          input.value = history[historyIdx];
        }
      } else if (e.key === "ArrowDown") {
        e.preventDefault();
        if (history.length && historyIdx >= 0) {
          historyIdx = Math.min(history.length - 1, historyIdx + 1);
          input.value = history[historyIdx];
        } else {
          input.value = "";
        }
      }
    };
    input.focus();
    document.getElementById("term-clear").onclick = () => {
      document.getElementById("term-output").textContent = "";
    };
  }

  async function runCommand(text) {
    text = text.trim();
    if (!text) return;
    const out = document.getElementById("term-output");
    const input = document.getElementById("term-input");
    history.push(text);
    historyIdx = -1;
    input.value = "";
    out.innerHTML += `\n<span class="prompt">$ ${esc(text)}</span>`;
    out.scrollTop = out.scrollHeight;
    try {
      const res = await API.post("/api/console/run", { command: text });
      if (res.output) out.innerHTML += `\n${esc(res.output)}`;
      if (res.error) out.innerHTML += `\n<span class="err">${esc(res.error)}</span>`;
      out.innerHTML += `\n<span class="text-faint">⎔ ${res.duration ? fmtDuration(res.duration) : ""}${res.api ? " · API" : " · CLI"} · exit ${res.exit_code}</span>`;
    } catch (e) {
      out.innerHTML += `\n<span class="err">${esc(e.message)}</span>`;
    }
    out.scrollTop = out.scrollHeight;
  }

  window.Pages = window.Pages || {};
  window.Pages.terminal = { render };
})();