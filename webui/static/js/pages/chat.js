/* Chat page — streaming conversation with Ollama models (SSE). */
(function () {
  let messages = [];
  let currentModel = "";
  let models = [];
  let busy = false;          // a generation is in flight
  let aborter = null;        // AbortController for the in-flight request

  async function render(el) {
    try {
      const data = await API.get("/api/ollama/models");
      models = data.models || [];
      if (models.length && !currentModel) currentModel = models[0].name;
    } catch { models = []; }
    el.innerHTML = `
      <div class="card">
        <div class="card-title">
          <span>💬 Chat</span>
          <span class="right">
            <select id="chat-model" style="padding:4px 8px;font-size:12px"${busy ? " disabled" : ""}>${models.map(m => `<option value="${esc(m.name)}"${m.name === currentModel ? " selected" : ""}>${esc(m.name)}</option>`).join("")}</select>
            <button class="btn-sm" id="chat-clear">✕ Clear</button>
          </span>
        </div>
        <div class="terminal" id="chat-output" style="min-height:300px;max-height:500px;overflow-y:auto">
          <span class="text-faint">Select a model and start chatting</span>
        </div>
        <div class="terminal-input">
          <span class="prompt">You</span>
          <input type="text" id="chat-input" autocomplete="off" placeholder="Type a message..." ${busy ? "disabled" : ""} />
          <button class="btn-sm" id="chat-send">Send</button>
        </div>
        <div id="chat-status" class="text-faint" style="margin-top:6px;font-size:12px"></div>
      </div>`;
    document.getElementById("chat-model").onchange = (e) => { currentModel = e.target.value; };
    document.getElementById("chat-clear").onclick = clearChat;
    const input = document.getElementById("chat-input");
    const sendBtn = document.getElementById("chat-send");
    sendBtn.onclick = () => sendOrStop(input);
    input.onkeydown = (e) => { if (e.key === "Enter") sendOrStop(input); };
    input.focus();
  }

  function clearChat() {
    if (busy) return; // stopping is the explicit way out
    messages = [];
    document.getElementById("chat-output").textContent = "";
    const status = document.getElementById("chat-status");
    if (status) status.textContent = "";
  }

  /* One entry point for the Send/Stop button: dispatch on busy flag. */
  function sendOrStop(input) {
    if (busy) { stopGeneration(); return; }
    sendMessage(input);
  }

  function stopGeneration() {
    if (aborter) aborter.abort();
  }

  function setBusy(on, input, sendBtn) {
    busy = on;
    if (sendBtn) sendBtn.textContent = on ? "Stop" : "Send";
    if (input) input.disabled = on;
    const modelSel = document.getElementById("chat-model");
    if (modelSel) modelSel.disabled = on;
  }

  async function sendMessage(input) {
    const text = input.value.trim();
    if (!text || !currentModel) return;
    input.value = "";
    const out = document.getElementById("chat-output");
    const status = document.getElementById("chat-status");
    const sendBtn = document.getElementById("chat-send");
    messages.push({ role: "user", content: text });
    // First message replaces the placeholder hint; later ones append.
    if (messages.length === 1) out.textContent = "";
    out.innerHTML += `\n<span class="prompt">You</span>\n${esc(text)}\n`;
    out.scrollTop = out.scrollHeight;

    // Streaming reply: header + markdown body node; deltas append to the
    // raw text buffer and re-render through the markdown pipeline.
    const head = document.createElement("span");
    head.textContent = "Assistant";
    head.className = "prompt";
    out.appendChild(head);
    out.appendChild(document.createTextNode("\n"));
    const bodyNode = document.createElement("span");
    bodyNode.className = "ok chat-md";
    out.appendChild(bodyNode);
    const cursor = document.createElement("span");
    cursor.className = "chat-cursor";
    cursor.textContent = "▍";
    bodyNode.appendChild(cursor);
    out.scrollTop = out.scrollHeight;

    setBusy(true, input, sendBtn);
    status.textContent = "⏳ generating…";
    aborter = new AbortController();
    let reply = "";
    let stats = null;
    try {
      const csrfCookie = document.cookie.split("; ").find((c) => c.startsWith("csrf="));
      const headers = { "Content-Type": "application/json" };
      if (csrfCookie) headers["X-CSRF-Token"] = csrfCookie.split("=")[1];
      const res = await fetch("/api/chat/stream", {
        method: "POST",
        credentials: "same-origin",
        headers,
        body: JSON.stringify({ model: currentModel, messages: messages }),
        signal: aborter.signal,
      });
      if (!res.ok) {
        let detail = `HTTP ${res.status}`;
        try {
          const data = await res.json();
          if (data && data.detail) detail = typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail);
        } catch { /* not json — keep the HTTP status detail */ }
        throw new Error(detail);
      }
      // Manual SSE parsing: events separated by a blank line; "event:" and
      // "data:" lines within. Ollama can pause for many seconds during prompt
      // eval, so we simply await each chunk without idle timeouts.
      const reader = res.body.getReader();
      const dec = new TextDecoder();
      let buf = "";
      const handleEvent = (evText) => {
        let ev = "message", data = "";
        for (const line of evText.split("\n")) {
          if (line.startsWith("event:")) ev = line.slice(6).trim();
          else if (line.startsWith("data:")) data += line.slice(5).trim();
        }
        if (ev === "delta") {
          try {
            const d = JSON.parse(data);
            if (d.content) {
              reply += d.content;
              cursor.remove();
              // Re-render the accumulated buffer as Markdown (sanitized).
              // Re-parse per delta is fine at chat scale and keeps the
              // visible output consistent with the final rendering.
              bodyNode.innerHTML = renderMarkdown(reply);
              bodyNode.appendChild(cursor);
              out.scrollTop = out.scrollHeight;
            }
          } catch { /* malformed chunk — skip */ }
        } else if (ev === "done") {
          try { stats = JSON.parse(data); } catch { stats = null; }
        } else if (ev === "error") {
          let msg = "stream error";
          try { msg = JSON.parse(data).error || msg; } catch { /* keep default */ }
          throw new Error(msg);
        }
      };
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let idx;
        while ((idx = buf.indexOf("\n\n")) !== -1) {
          const evText = buf.slice(0, idx);
          buf = buf.slice(idx + 2);
          if (evText.trim()) handleEvent(evText);
        }
      }
      if (buf.trim()) handleEvent(buf);
      // Persist the full reply even if the stream was stopped midway.
      if (reply) messages.push({ role: "assistant", content: reply });
      const info = [];
      if (stats && stats.model) info.push(`model: ${stats.model}`);
      if (stats && stats.eval_count) info.push(`tokens: ${stats.eval_count}`);
      if (stats && stats.total_duration) info.push(`duration: ${fmtDuration(stats.total_duration / 1e9)}`);
      if (busy) {
        status.textContent = info.join(" · ") || "";
      }
    } catch (e) {
      if (e.name === "AbortError") {
        status.textContent = "⏹ stopped";
        if (reply) messages.push({ role: "assistant", content: reply });
      } else {
        out.innerHTML += `\n<span class="err">⚠ ${esc(e.message)}</span>`;
        status.textContent = "error";
        if (reply) {
          // Keep partial output: persist what arrived before the failure.
          messages.push({ role: "assistant", content: reply });
        } else {
          // Roll back the user message so the next send doesn't poison context.
          messages = messages.filter((m, i) => !(i === messages.length - 1 && m.role === "user"));
        }
      }
    } finally {
      cursor.remove();
      // Final markdown render without the cursor (also normalizes any
      // trailing partial syntax the last delta may have left open).
      if (reply) bodyNode.innerHTML = renderMarkdown(reply);
      setBusy(false, input, sendBtn);
      aborter = null;
      out.scrollTop = out.scrollHeight;
      input.focus();
    }
  }
  window.Pages = window.Pages || {};
  window.Pages.chat = { render, sendMessage: () => sendMessage(document.getElementById("chat-input")), stopGeneration, clearChat };
})();
