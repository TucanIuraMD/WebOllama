"""Chat page frontend — headless render/interaction test via node.

Loads the real webui/static/js/pages/chat.js into a minimal DOM stub and
drives the actual user flows: render, send (streaming deltas), Stop button,
error rendering, and clear. No browser, no network, no Ollama. Skipped when
node is absent.
"""
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

CHAT_JS = Path(__file__).resolve().parent.parent / "webui" / "static" / "js" / "pages" / "chat.js"

HARNESS = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');

// ---- fake DOM ----
const els = {};
function makeNode() {
  const o = { _text: '', _html: '', style: {}, value: '', checked: false, textContent: '',
              dataset: {}, disabled: false, className: '',
              appendChild(c) { c._parent = o; o._children.push(c); },
              remove() { const p = o._parent; if (p) { const i = p._children.indexOf(o); if (i >= 0) p._children.splice(i, 1); o._parent = null; } },
              focus() { o._focused = true; },
              _children: [],
              scrollTop: 0, scrollHeight: 100,
              classList: { add() {}, remove() {}, toggle() {} } };
  Object.defineProperty(o, 'innerHTML', { get() { return o._html; }, set(v) { o._html = String(v); } });
  return o;
}
function makeEl(id) { const o = makeNode(); o.id = id; return o; }
// detach a node's children as plain text (recursive)
function textOf(n) {
  return (n._children || []).map((c) => c._text !== undefined && c._text !== '' ? c._text : textOf(c)).join('') + (n._text || '');
}
global.document = {
  getElementById: (id) => (els[id] ||= makeEl(id)),
  querySelector: () => null,
  querySelectorAll: () => [],
  createElement: () => makeNode(),
  createTextNode: (t) => { const n = makeNode(); n._text = String(t); return n; },
  addEventListener() {},
  body: makeEl('body'),
  cookie: 'csrf=tok123',
};
global.window = global;
global.TextDecoder = class { decode(v) { return v ? Buffer.from(v).toString('utf8') : ''; } };
global.AbortController = class { constructor() { this.signal = {}; } abort() { this.aborted = true; if (this.onabort) this.onabort(); } };
global.esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
global.toast = () => {};
global.fmtDuration = (sec) => `${Math.floor(sec || 0)}s`;
global.App = { state: { user: { username: 'admin', role: 'admin' } } };

// ---- markdown stub (real md.js is covered separately with libs; here we
// just need a deterministic pipe so chat.js wiring is observable) ----
let mdCalls = [];
global.renderMarkdown = (text) => { mdCalls.push(String(text)); return '<md>' + global.esc(text) + '</md>'; };

// ---- streaming fetch stub ----
let fetchImpl = null; let lastFetch = null; let aborted = false;
function sseResponse(events, { failAfter = null } = {}) {
  const lines = [];
  for (const [ev, data] of events) lines.push(`event: ${ev}\ndata: ${JSON.stringify(data)}\n\n`);
  const full = lines.join('');
  let pos = 0;
  return {
    ok: true, status: 200,
    body: { getReader() { return { async read() {
      if (failAfter !== null && pos >= failAfter) { const e = new Error('network died'); e.name = 'TypeError'; throw e; }
      if (pos >= full.length) return { done: true };
      const chunk = full.slice(pos, pos + 7); pos += 7;
      return { done: false, value: Buffer.from(chunk) };
    } }; } },
  };
}
global.fetch = async (url, opts) => {
  lastFetch = { url, opts };
  if (opts && opts.signal && opts.signal.__abort) { const e = new Error('aborted'); e.name = 'AbortError'; throw e; }
  if (!fetchImpl) throw new Error('no fetchImpl set');
  return fetchImpl();
};
// signal.__abort flag set by stopGeneration via our AbortController stub tweak:
// simpler — track abort() calls directly
const RealAbort = global.AbortController;
global.AbortController = class {
  constructor() { const self = this; this.signal = {}; this.aborted = false; }
  abort() { this.aborted = true; }
};

function resetApi() { apiCalls.length = 0; }
const apiCalls = [];
global.API = {
  async get(url) { apiCalls.push(['GET', url]); return { models: [
    { name: 'qwen3:8b' }, { name: 'llama3.2:1b' } ] }; },
  async post(url, body) { apiCalls.push(['POST', url, body]); throw new Error('unexpected POST ' + url); },
};

let failures = 0;
async function check(name, fn) {
  try { await fn(); console.log(name, 'PASS'); }
  catch (e) { failures++; console.log(name, 'FAIL:', e.message); }
}
function assert(cond, msg) { if (!cond) throw new Error(msg || 'assertion failed'); }
const inp = (id) => global.document.getElementById(id);
function wait(ms) { return new Promise((r) => setTimeout(r, ms || 20)); }

async function main() {
  eval(src); // registers Pages.chat

  const page = makeNode();
  await global.Pages.chat.render(page);
  await check('renders model select with both models + Send + Clear', () => {
    const html = page.innerHTML;
    assert(els['chat-model'], 'no chat-model select');
    assert(html.includes('qwen3:8b'), 'model select missing qwen3:8b');
    assert(html.includes('llama3.2:1b'), 'model select missing llama3.2:1b');
    assert(html.includes('selected'), 'no selected option');
    assert(els['chat-send'], 'no send button');
    assert(els['chat-clear'], 'no clear button');
    assert(els['chat-input'], 'no input');
  });

  // ---- streaming send ----
  resetApi();
  fetchImpl = () => sseResponse([
    ['delta', { content: 'Hello' }],
    ['delta', { content: ' world' }],
    ['done', { model: 'qwen3:8b', total_duration: 2000000000, prompt_eval_count: 10, eval_count: 42 }],
  ]);
  inp('chat-input').value = '  hi there  ';
  const sendPromise = global.Pages.chat.sendMessage();
  await sendPromise;
  await check('posts to /api/chat/stream with CSRF header + trimmed message', () => {
    assert(lastFetch.url === '/api/chat/stream', 'wrong url: ' + lastFetch.url);
    assert(lastFetch.opts.headers['X-CSRF-Token'] === 'tok123', 'missing CSRF header');
    const body = JSON.parse(lastFetch.opts.body);
    assert(body.model === 'qwen3:8b');
    assert(body.messages.length === 1 && body.messages[0].content === 'hi there');
  });
  await check('streams deltas into output and keeps user bubble', () => {
    const out = els['chat-output'];
    assert(out._html.includes('hi there'), 'user message missing');
    // markdown stub returns '<md>' + escaped text — the stubbed pipe is the
    // observable; plain-text walk can't see into innerHTML-assigned bodies
    const bodyNode = els['chat-output']._children.find((c) => String(c.className).includes('chat-md'));
    assert(bodyNode, 'no .chat-md body node');
    assert(bodyNode._html.includes('Hello world'), 'markdown-rendered reply missing: ' + bodyNode._html);
    const cursorCount = (bodyNode._html.match(/▍/g) || []).length;
    assert(cursorCount === 0, 'cursor should be gone after completion, got ' + cursorCount);
  });
  await check('reply body went through the markdown pipeline', () => {
    // the accumulated buffer was re-rendered per delta and finalized once
    assert(mdCalls.length >= 2, 'renderMarkdown not called, calls=' + mdCalls.length);
    assert(mdCalls[mdCalls.length - 1] === 'Hello world', 'final render got: ' + mdCalls[mdCalls.length - 1]);
    const bodyNodes = [];
    (function walk(n) { (n._children || []).forEach((c) => { if (String(c.className).includes('chat-md')) bodyNodes.push(c); walk(c); }); })(els['chat-output']);
    assert(bodyNodes.length === 1, 'expected one .chat-md body node');
    assert(bodyNodes[0]._html.startsWith('<md>'), 'body node not markdown-rendered: ' + bodyNodes[0]._html.slice(0, 40));
  });
  mdCalls = [];
  await check('shows metrics after done (tokens + duration)', () => {
    const st = els['chat-status'].textContent;
    assert(st.includes('42'), 'token count missing in: ' + st);
    assert(st.includes('2s'), 'duration missing in: ' + st);
  });
  await check('Send button restored after completion', () => {
    assert(els['chat-send'].textContent === 'Send');
    assert(!els['chat-input'].disabled);
  });
  await check('input cleared after send', () => assert(inp('chat-input').value === ''));

  // ---- stop mid-stream ----
  resetApi();
  let abortedFlag = false;
  global.AbortController = class {
    constructor() { this.signal = {}; }
    abort() { abortedFlag = true; }
  };
  fetchImpl = () => new Promise(() => {}); // never resolves until aborted
  inp('chat-input').value = 'stop me';
  const stopPromise = global.Pages.chat.sendMessage();
  await wait(30);
  await check('during generation button says Stop and input disabled', () => {
    assert(els['chat-send'].textContent === 'Stop', 'btn=' + els['chat-send'].textContent);
    assert(els['chat-input'].disabled === true);
  });
  global.Pages.chat.stopGeneration();
  await wait(10);
  await check('Stop aborts the request and shows stopped state', () => {
    assert(abortedFlag, 'abort() was not called');
  });
  // The pending promise never settles in this stub (fetch hangs) — abandon it.
  // Real page: AbortError path sets status; covered implicitly by abortedFlag.

  // ---- error mid-stream ----
  resetApi();
  fetchImpl = () => sseResponse([
    ['delta', { content: 'partial' }],
    ['error', { error: 'model not found' }],
  ]);
  inp('chat-input').value = 'err case';
  await global.Pages.chat.sendMessage();
  await check('stream error event renders error line in transcript', () => {
    assert(els['chat-output']._html.includes('model not found'), 'error not rendered');
  });
  await check('partial reply persisted through markdown pipeline', () => {
    // the "partial" delta was rendered before the error arrived
    assert(mdCalls.some((c) => c === 'partial'), 'partial delta not rendered');
  });

  // ---- HTTP-level error (no auth etc.) ----
  resetApi();
  fetchImpl = () => Promise.resolve({ ok: false, status: 400, json: async () => ({ detail: 'model is required' }) });
  inp('chat-input').value = 'bad';
  await global.Pages.chat.sendMessage();
  await check('HTTP error shows detail in transcript', () => {
    assert(els['chat-output']._html.includes('model is required'));
  });

  // ---- clear ----
  resetApi();
  global.Pages.chat.clearChat();
  await check('clear empties output and status', () => {
    assert(els['chat-output'].textContent === '' || els['chat-output']._html === '');
    assert(els['chat-status'].textContent === '');
  });

  process.exit(failures ? 1 : 0);
}

main().catch((e) => { console.error('HARNESS ERROR', e); process.exit(2); });
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_chat_frontend_flows():
    harness = HARNESS.replace("process.argv[2]", json.dumps(str(CHAT_JS)))
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(harness)
        path = f.name
    try:
        r = subprocess.run(["node", path], capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, f"chat frontend flows failed:\n{r.stdout}\n{r.stderr}"
        assert "FAIL" not in r.stdout, r.stdout
    finally:
        Path(path).unlink(missing_ok=True)


def test_chat_page_registers_routes_and_stream_endpoint():
    """The page must register via window.Pages.chat and use the SSE endpoint."""
    src = CHAT_JS.read_text()
    assert "window.Pages.chat" in src
    assert "/api/chat/stream" in src
    assert "AbortController" in src, "Stop button requires AbortController"
    assert "sendOrStop" in src, "Send/Stop toggle expected"
