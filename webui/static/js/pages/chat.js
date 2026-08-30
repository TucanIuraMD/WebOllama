/* Chat page — send messages to Ollama models */
(function () {
  let messages = [];
  let currentModel = "";
  let models = [];

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
            <select id="chat-model" style="padding:4px 8px;font-size:12px">${models.map(m => `<option value="${esc(m.name)}"${m.name === currentModel ? " selected" : ""}>${esc(m.name)}</option>`).join("")}</select>
            <button class="btn-sm" id="chat-clear">✕ Clear</button>
          </span>
        </div>
        <div class="terminal" id="chat-output" style="min-height:300px;max-height:500px;overflow-y:auto">
          <span class="text-faint">Select a model and start chatting</span>
        </div>
        <div class="terminal-input">
          <span class="prompt">You</span>
          <input type="text" id="chat-input" autocomplete="off" placeholder="Type a message..." />
          <button class="btn-sm" id="chat-send">Send</button>
        </div>
        <div id="chat-status" class="text-faint" style="margin-top:6px;font-size:12px"></div>
      </div>`;
    document.getElementById("chat-model").onchange = (e) => { currentModel = e.target.value; };
    document.getElementById("chat-clear").onclick = () => { messages = []; document.getElementById("chat-output").textContent = ""; };
    const input = document.getElementById("chat-input");
    const sendBtn = document.getElementById("chat-send");
    const send = () => sendMessage(input);
    sendBtn.onclick = send;
    input.onkeydown = (e) => { if (e.key === "Enter") send(); };
    input.focus();
  }

  async function sendMessage(input) {
    const text = input.value.trim();
    if (!text || !currentModel) return;
    input.value = "";
    const out = document.getElementById("chat-output");
    const status = document.getElementById("chat-status");
    messages.push({ role: "user", content: text });
    out.innerHTML += `\n<span class="prompt">You</span>\n${esc(text)}`;
    out.scrollTop = out.scrollHeight;
    status.textContent = "⏳ waiting for response...";
    try {
      const res = await API.post("/api/chat/run", { model: currentModel, messages: messages });
      if (res.ok && res.message) {
        const reply = res.message.content;
        messages.push({ role: "assistant", content: reply });
        out.innerHTML += `\n<span class="ok">Assistant</span>\n${esc(reply)}`;
        const info = [];
        if (res.model) info.push(`model: ${res.model}`);
        if (res.eval_count) info.push(`tokens: ${res.eval_count}`);
        if (res.total_duration) info.push(`duration: ${fmtDuration(res.total_duration / 1e9)}`);
        status.textContent = info.join(" · ");
      } else { status.textContent = "⚠ unexpected response"; }
    } catch (e) {
      out.innerHTML += `\n<span class="err">⚠ ${esc(e.message)}</span>`;
      status.textContent = "error";
    }
    out.scrollTop = out.scrollHeight;
    input.focus();
  }
  window.Pages = window.Pages || {};
  window.Pages.chat = { render };
})();
