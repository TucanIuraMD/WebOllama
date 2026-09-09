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
    // Deep-link preselect: #chat?model=<name> (e.g. from the Running page).
    // Only applied when the requested model exists in the model list.
    const requested = App.state && App.state.pageQuery && App.state.pageQuery.model;
    if (requested && models.some((m) => m.name === requested)) currentModel = requested;
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
          <textarea id="chat-input" rows="1" autocomplete="off" placeholder="Type a message... (Enter — send, Shift+Enter — newline)" ${busy ? "disabled" : ""}></textarea>
          <button class="btn-sm" id="chat-send">Send</button>
        </div>
        <div id="chat-status" class="text-faint" style="margin-top:6px;font-size:12px"></div>
      </div>`;
    document.getElementById("chat-model").onchange = (e) => { currentModel = e.target.value; };
    document.getElementById("chat-clear").onclick = clearChat;
    const input = document.getElementById("chat-input");
    const sendBtn = document.getElementById("chat-send");
    sendBtn.onclick = () => sendOrStop(input);
    // Enter sends, Shift+Enter inserts a newline (multi-line composer —
    // required for "Reply with code" editing before sending).
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        sendOrStop(input);
      }
    });
    // keep the composer growing with its content (up to ~10 lines)
    input.addEventListener("input", () => autosizeComposer(input));
    input.focus();
  }

  /* Grow the composer with content; shrink when emptied. */
  function autosizeComposer(input) {
    if (!input || input.tagName !== "TEXTAREA") return;
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 220) + "px";
  }

  /* ======================================================================
   * CODE BLOCK ACTIONS (Save / Reply)
   * ----------------------------------------------------------------------
   * For every <pre> rendered by the Markdown pipeline we add ONE action
   * bar ABOVE and ONE BELOW the code (so actions are reachable without
   * scrolling long blocks). The <pre> content is never mutated — the DOM
   * stays exactly what marked+DOMPurify produced.
   *
   * Rendering strategy (streaming-safe):
   * - chat.js re-renders the whole reply per delta (innerHTML = ...), so
   *   per-render decoration is inherently idempotent: no duplicate bars
   *   can accumulate across deltas — each render discards the previous
   *   DOM subtree wholesale.
   * - Handlers are attached ONLY to the freshly created action buttons
   *   (never delegated on the transcript root), so repeated decoration
   *   cannot stack listeners on surviving nodes → no listener leaks.
   * - decorateCodeActions() is idempotent for ANY foreign markup (it
   *   skips pre's that already carry a code-actions bar) — safe to call
   *   after any future single-node update path.
   * ==================================================================== */

  const CB = { BAR: "code-actions", REPLY: "chat-input" };

  /* Map a fenced-block language tag to a (filename, extension) pair.
   * Unknown/empty tags degrade to plain text. Never guesses code. */
  const LANG_MAP = {
    python: ["script", ".py"], py: ["script", ".py"],
    javascript: ["script", ".js"], js: ["script", ".js"], node: ["script", ".js"],
    typescript: ["script", ".ts"], ts: ["script", ".ts"],
    bash: ["script", ".sh"], sh: ["script", ".sh"], shell: ["script", ".sh"],
    zsh: ["script", ".sh"], console: ["script", ".sh"],
    json: ["data", ".json"], yaml: ["config", ".yaml"], yml: ["config", ".yml"],
    toml: ["config", ".toml"], ini: ["config", ".ini"],
    html: ["page", ".html"], xml: ["doc", ".xml"], css: ["style", ".css"],
    sql: ["query", ".sql"], rust: ["main", ".rs"], rs: ["main", ".rs"],
    go: ["main", ".go"], golang: ["main", ".go"],
    c: ["main", ".c"], cpp: ["main", ".cpp"], "c++": ["main", ".cpp"],
    java: ["Main", ".java"], kotlin: ["Main", ".kt"], kt: ["Main", ".kt"],
    cs: ["Program", ".cs"], "c#": ["Program", ".cs"],
    ruby: ["script", ".rb"], rb: ["script", ".rb"],
    php: ["script", ".php"], perl: ["script", ".pl"], pl: ["script", ".pl"],
    lua: ["script", ".lua"], r: ["script", ".r"], matlab: ["script", ".m"],
    markdown: ["note", ".md"], md: ["note", ".md"],
    diff: ["patch", ".diff"], patch: ["patch", ".patch"],
    dockerfile: ["Dockerfile", ""], makefile: ["Makefile", ""],
  };

  function langOf(codeEl) {
    // marked emits <code class="language-xxx">; take the FIRST language
    // token only (a tag like "c++" has no spaces, others never either).
    const m = /\blanguage-([\w+#-]+)/.exec(codeEl && codeEl.className || "");
    return m ? m[1].toLowerCase() : "";
  }

  function fileNameFor(lang) {
    const hit = LANG_MAP[lang];
    return hit ? hit[0] + hit[1] : "snippet.txt";  // unknown → .txt
  }

  /* The raw code text of a pre, exactly as rendered (fences were consumed
   * by marked; textContent gives the code WITHOUT ``` and language tag). */
  function codeTextOf(pre) {
    const code = pre.querySelector("code");
    // marked puts the payload in <code>; fall back to the pre itself
    const raw = code ? code.textContent : pre.textContent;
    // strip ONE trailing newline that HTML parsing adds after the last line
    return raw.replace(/\n$/, "");
  }

  /* Client-only download (Blob + object URL); no server round-trip. */
  function downloadCode(pre) {
    const lang = langOf(pre.querySelector("code"));
    const name = fileNameFor(lang);
    const blob = new Blob([codeTextOf(pre)], { type: "text/plain;charset=utf-8" });
    let url = null;
    try {
      url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = name;
      document.body.appendChild(a);
      a.click();
      a.remove();
    } finally {
      if (url) setTimeout(() => URL.revokeObjectURL(url), 1000);
    }
  }

  /* Copy THIS block's code (textContent of its <code>) to the clipboard:
   * no fences, no language tag, no server round-trip.
   * navigator.clipboard exists only in secure contexts — on plain http
   * (e.g. LAN hosts like Hermes) we fall back to the legacy
   * execCommand('copy') path. Both failures are non-fatal: the button
   * shows «Ошибка», the chat keeps working. */
  function copyCode(pre, btn) {
    const text = codeTextOf(pre);
    const done = (ok) => {
      if (!btn) return;
      const prev = btn.textContent;
      btn.textContent = ok ? "Скопировано" : "Ошибка";
      btn.classList.toggle("cb-ok", !!ok);
      btn.classList.toggle("cb-err", !ok);
      setTimeout(() => {
        btn.textContent = prev;
        btn.classList.remove("cb-ok", "cb-err");
      }, 1500);
    };
    const legacy = () => {
      try {
        const ta = document.createElement("textarea");
        ta.value = text;
        ta.setAttribute("readonly", "");
        ta.style.position = "fixed";
        ta.style.top = "-1000px";
        document.body.appendChild(ta);
        ta.select();
        const ok = document.execCommand ? document.execCommand("copy") : false;
        ta.remove();
        done(!!ok);
      } catch { done(false); }
    };
    try {
      if (navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
        navigator.clipboard.writeText(text).then(() => done(true), legacy);
      } else {
        legacy();
      }
    } catch { legacy(); }
  }

  /* Reply-with-code: put a fenced quote of THIS block into the composer
   * for the user to edit. Never auto-sends (sendOrStop is not invoked);
   * composer content is set via .value → no HTML/script can execute. */
  function replyWithCode(pre) {
    const input = document.getElementById(CB.REPLY);
    if (!input) return;
    const lang = langOf(pre.querySelector("code"));
    const text = codeTextOf(pre);
    input.value = "Про этот код:\n```" + lang + "\n" + text + "\n```\n";
    input.focus();
    // move caret to the end (after the fence) so the user types below it
    try { input.setSelectionRange(input.value.length, input.value.length); } catch { /* not focusable */ }
    autosizeComposer(input);
    const status = document.getElementById("chat-status");
    if (status) status.textContent = "Code added to the composer — edit it, then press Send.";
  }

  function barHtml() {
    return '<span class="' + CB.BAR + '">' +
      '<button type="button" class="cb-btn" data-cb-action="copy">Копировать</button>' +
      '<button type="button" class="cb-btn" data-cb-action="save">Сохранить</button>' +
      '<button type="button" class="cb-btn" data-cb-action="reply">Ответить</button>' +
      "</span>";
  }

  /* Decorate every code block inside root with top+bottom action bars.
   * Idempotent; never mutates the rendered <pre> content itself. */
  function decorateCodeActions(root) {
    if (!root || typeof root.querySelectorAll !== "function") return 0;
    let n = 0;
    // direct children only — nested pre's do not exist in marked output
    for (const pre of root.querySelectorAll("pre")) {
      if (!pre) continue;
      if (pre.querySelector("." + CB.BAR)) continue;  // already decorated
      // fresh bars per render → handlers die with the discarded subtree
      const barTop = document.createElement("span");
      barTop.innerHTML = barHtml();
      const barBottom = document.createElement("span");
      barBottom.innerHTML = barHtml();
      pre.insertBefore(barTop.firstChild, pre.firstChild);
      pre.appendChild(barBottom.firstChild);
      for (const btn of pre.querySelectorAll("[data-cb-action]")) {
        btn.addEventListener("click", (ev) => {
          ev.stopPropagation();
          const host = btn.closest("pre");
          if (!host) return;
          if (btn.dataset.cbAction === "copy") copyCode(host, btn);
          else if (btn.dataset.cbAction === "save") downloadCode(host);
          else replyWithCode(host);
        });
      }
      n++;
    }
    return n;
  }

  /* Wire a freshly rendered markdown body (per-delta AND final): replaces
   * the cursor-safe innerHTML assignment with render + decorate. */
  function renderChatBody(bodyNode, text) {
    bodyNode.innerHTML = renderMarkdown(text);
    decorateCodeActions(bodyNode);
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
              renderChatBody(bodyNode, reply);
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
      // trailing partial syntax the last delta may have left open) and
      // re-wire code block actions on the final DOM.
      if (reply) renderChatBody(bodyNode, reply);
      setBusy(false, input, sendBtn);
      aborter = null;
      out.scrollTop = out.scrollHeight;
      input.focus();
    }
  }
  window.Pages = window.Pages || {};
  window.Pages.chat = {
    render, sendMessage: () => sendMessage(document.getElementById("chat-input")), stopGeneration, clearChat,
    // code block actions (exposed for tests / debugging)
    decorateCodeActions, fileNameFor, langOf, codeTextOf, copyCode,
  };
})();
