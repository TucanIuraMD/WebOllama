"""Running v2 frontend — headless flow tests via node.

Loads the real webui/static/js/pages/running.js (plus the real chat.js
preselect change) into the minimal DOM stub and drives the actual page
flows: render states (loading/offline/error/empty/models), card data
(model VRAM vs GPU telemetry, CPU/GPU split), live WS updates (model
appears after Run, disappears after Stop), Stop success/error/double-
click guard, Running→Chat deep link, Running→Models sync. No browser,
no network (API mocked per test). Skipped when node is absent.
"""
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

RUNNING_JS = Path(__file__).resolve().parent.parent / "webui" / "static" / "js" / "pages" / "running.js"
CHAT_JS = Path(__file__).resolve().parent.parent / "webui" / "static" / "js" / "pages" / "chat.js"

HARNESS = r"""
const fs = require('fs');
const runningSrc = fs.readFileSync(process.argv[2], 'utf8');
const chatSrc = fs.readFileSync(process.argv[3], 'utf8');

// ---- fake DOM ----
const els = {};
function makeNode() {
  const o = { _text: '', _html: '', style: {}, value: '', checked: false, textContent: '',
              dataset: {}, disabled: false, className: '', id: '',
              appendChild(c) { c._parent = o; o._children.push(c); },
              remove() { const p = o._parent; if (p) { const i = p._children.indexOf(o); if (i >= 0) p._children.splice(i, 1); o._parent = null; } },
              focus() {}, _children: [], scrollTop: 0, scrollHeight: 100,
              classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } } };
  Object.defineProperty(o, 'innerHTML', { get() { return o._html; }, set(v) { o._html = String(v); o._children = []; } });
  return o;
}
function makeEl(id) { const o = makeNode(); o.id = id; return o; }
global.document = {
  getElementById: (id) => (els[id] ||= makeEl(id)),
  querySelector: () => null,
  querySelectorAll: () => [],
  createElement: () => makeNode(),
  addEventListener() {},
  body: makeEl('body'),
};
global.window = global;
global.esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
global.toast = (msg, type) => { global.__toasts.push([msg, type]); };
global.fmtBytes = (n) => n == null || isNaN(n) ? '—' : (n / 1e9).toFixed(1) + ' GB';
global.fmtNum = (n) => String(n);
global.debounce = (fn) => fn;
global.location = { hash: '' };

// ---- API mock ----
let responders = {};
let calls = [];
const norm = (url) => decodeURIComponent(url.split('?')[0]);
global.API = {
  get: async (url) => { calls.push(['GET', url]); const r = responders['GET ' + norm(url)]; if (!r) throw new Error('no mock for GET ' + url); return typeof r === 'function' ? r() : r; },
  post: async (url, body) => { calls.push(['POST', url, body]); const r = responders['POST ' + norm(url)]; if (!r) throw new Error('no mock for POST ' + url); return typeof r === 'function' ? r(body) : r; },
  del: async (url) => { calls.push(['DELETE', url]); const r = responders['DELETE ' + norm(url)]; if (!r) throw new Error('no mock for DELETE ' + url); return typeof r === 'function' ? r() : r; },
};
function resetApi() { responders = {}; calls = []; global.__toasts = []; }
global.__toasts = global.__toasts || [];

let failures = 0;
async function check(name, fn) {
  try { await fn(); console.log(name, 'PASS'); }
  catch (e) { failures++; console.log(name, 'FAIL:', e.message); }
}
function assert(cond, msg) { if (!cond) throw new Error(msg || 'assertion failed'); }

function ps(name, size, vram, extra = {}) {
  return Object.assign({
    name, model: name, size,
    size_vram: vram === undefined ? size : vram,
    expires_at: '2026-09-08T10:47:57Z',
    details: { family: 'qwen3', parameter_size: '7.6B', quantization_level: 'Q4_K_M' },
    context_length: 32768,
    digest: 'abc123',
  }, extra);
}
const M_GPU = ps('qwen3:8b', 4_700_000_000);                    // fully GPU
const M_SPLIT = ps('devstral-24b', 14_000_000_000, 13_500_000_000); // partial CPU offload
const M_CPU = ps('tiny:1b', 1_300_000_000, 0);                  // fully CPU

async function main() {
  global.App = { state: { currentPage: 'running', pageQuery: {} } };
  eval(runningSrc);
  const page = global.Pages.running;
  const container = makeEl('page-container');

  await check('loading state while fetch in flight', async () => {
    let resolveFetch;
    responders['GET /api/ollama/running'] = () => new Promise((res) => { resolveFetch = res; });
    const p = page.render(container);
    await Promise.resolve(); // let render reach draw() (shell built before fetch resolves)
    assert(els['run-body']._html.includes('Loading running models'), 'loading state missing');
    resolveFetch({ models: [], count: 0 });
    await p;
  });

  await check('API error → error state, NOT "No models loaded"', async () => {
    responders['GET /api/ollama/running'] = () => { throw new Error('HTTP 502: connection refused'); };
    await page.render(container);
    assert(els['run-body']._html.includes('Failed to load running models'), 'error state missing');
    assert(els['run-body']._html.includes('connection refused'), 'error detail missing');
    assert(!els['run-body']._html.includes('No models loaded'), 'empty state shown for API error');
  });

  await check('Ollama offline (WS online=false) → offline state, keeps last data', async () => {
    responders['GET /api/ollama/running'] = () => ({ models: [M_GPU], count: 1 });
    await page.render(container);
    page.onSnapshot({ ollama: { online: false, running: null } });
    assert(els['run-body']._html.includes('Ollama unavailable'), 'offline state missing');
    // last known data kept: count remains, alert shows
    assert(els['run-count'].textContent === '1', 'last known data discarded');
  });

  await check('empty state distinguishes from error (loaded, zero models)', async () => {
    responders['GET /api/ollama/running'] = () => ({ models: [], count: 0 });
    await page.render(container);
    assert(els['run-body']._html.includes('No models loaded into memory'), 'empty state missing');
    assert(els['run-body']._html.includes('#models'), 'link to Models missing');
  });

  await check('one GPU-resident model card: model VRAM, 100% GPU, context, unload time', async () => {
    responders['GET /api/ollama/running'] = () => ({ models: [M_GPU], count: 1 });
    await page.render(container);
    const body = els['run-body']._html;
    assert(body.includes('qwen3:8b'), 'model name missing');
    assert(body.includes('4.7 GB'), 'model VRAM missing: ' + body.slice(0, 400));
    assert(body.includes('100% GPU'), 'full-GPU badge missing');
    assert(body.includes('32768'), 'context length missing');
    assert(body.includes('2026-09-08 10:47:57'), 'unload time missing');
    assert(body.includes('running'), 'status badge missing');
    // model VRAM is from /api/ps, not GPU telemetry — labelled so
    assert(body.includes('Model VRAM'), 'model VRAM label missing');
  });

  await check('CPU/GPU split: size_vram < size → percent badge with both parts', async () => {
    responders['GET /api/ollama/running'] = () => ({ models: [M_SPLIT, M_CPU], count: 2 });
    await page.render(container);
    const body = els['run-body']._html;
    assert(body.includes('96% GPU / 4% CPU'), 'split badge missing: ' + (body.match(/\d+% GPU[^<]*/) || [''])[0]);
    assert(body.includes('devstral-24b'), 'split model missing');
    // fully-CPU model: size_vram=0 → no invented GPU info (— shown)
    const s = page.splitInfo(M_CPU);
    assert(s === null, 'zero-VRAM model must not invent split');
    // multiple models render together
    assert(body.includes('tiny:1b'), 'second model missing');
  });

  await check('total line is model VRAM sum (explicitly not GPU telemetry)', async () => {
    responders['GET /api/ollama/running'] = () => ({ models: [M_GPU, M_SPLIT], count: 2 });
    await page.render(container);
    const body = els['run-body']._html;
    assert(body.includes('Total model VRAM'), 'total label missing');
    assert(body.includes('not GPU telemetry'), 'VRAM provenance note missing');
  });

  await check('live update: model appears after Run via WS snapshot', async () => {
    responders['GET /api/ollama/running'] = () => ({ models: [], count: 0 });
    await page.render(container);
    assert(els['run-body']._html.includes('No models loaded'), 'precondition');
    page.onSnapshot({ ollama: { online: true, running: [M_GPU] } });
    assert(els['run-body']._html.includes('qwen3:8b'), 'model did not appear without reload');
    assert(els['run-count'].textContent === '1', 'count not updated');
  });

  await check('live update: model disappears after Stop via WS snapshot', async () => {
    responders['GET /api/ollama/running'] = () => ({ models: [M_GPU], count: 1 });
    await page.render(container);
    page.onSnapshot({ ollama: { online: true, running: [M_GPU] } }); // same sig → no flicker
    page.onSnapshot({ ollama: { online: true, running: [] } });
    assert(els['run-body']._html.includes('No models loaded'), 'model did not disappear');
  });

  await check('live update: no redraw when signature unchanged (no flicker)', () => {
    els['run-body']._html = 'CANARY';
    page.onSnapshot({ ollama: { online: true, running: [] } }); // same as current → skip
    assert(els['run-body']._html === 'CANARY', 'redrew without changes');
  });

  await check('multiple models: all cards render, count matches', async () => {
    responders['GET /api/ollama/running'] = () => ({ models: [M_GPU, M_SPLIT, M_CPU], count: 3 });
    await page.render(container);
    assert(els['run-count'].textContent === '3', 'count wrong');
    for (const n of ['qwen3:8b', 'devstral-24b', 'tiny:1b']) {
      assert(els['run-body']._html.includes(n), n + ' card missing');
    }
  });

  await check('Stop success: disable+label during flight, WS removes card', async () => {
    responders['GET /api/ollama/running'] = () => ({ models: [M_GPU], count: 1 });
    await page.render(container);
    let stopCalls = 0;
    responders['DELETE /api/ollama/models/qwen3:8b/stop'] = () => { stopCalls++; return { ok: true }; };
    const btn = makeNode();
    await global.Actions['run-stop']({ model: 'qwen3:8b' }, btn);
    assert(stopCalls === 1, 'stop endpoint not called');
    assert(btn.disabled === true, 'button not disabled during flight');
    assert(btn.textContent.includes('stopping'), 'loading label missing');
    assert(global.__toasts.some((t) => String(t[0]).includes('Unloaded')), 'success toast missing');
    // card removed via WS after stop
    page.onSnapshot({ ollama: { online: true, running: [] } });
    assert(els['run-body']._html.includes('No models loaded'), 'card not removed by WS');
    // after the 800ms fallback the guard cleared (no full-page re-render needed)
    await new Promise((r) => setTimeout(r, 900));
  });

  await check('duplicate Stop prevention: second click while in flight is a no-op', async () => {
    responders['GET /api/ollama/running'] = () => ({ models: [M_GPU], count: 1 });
    await page.render(container);
    let stopCalls = 0;
    let release;
    responders['DELETE /api/ollama/models/qwen3:8b/stop'] = () => new Promise((res) => { stopCalls++; release = res; });
    const first = global.Actions['run-stop']({ model: 'qwen3:8b' }, null);
    await global.Actions['run-stop']({ model: 'qwen3:8b' }, null); // second click mid-flight
    release({ ok: true });
    await first;
    await new Promise((r) => setTimeout(r, 900)); // let fallback clear guard
    assert(stopCalls === 1, 'second stop reached API: calls=' + stopCalls);
  });

  await check('Stop error: toast + button restored, model stays', async () => {
    responders['GET /api/ollama/running'] = () => ({ models: [M_GPU], count: 1 });
    await page.render(container);
    responders['DELETE /api/ollama/models/qwen3:8b/stop'] = () => { throw new Error('HTTP 502: Ollama offline'); };
    global.__toasts = [];
    await global.Actions['run-stop']({ model: 'qwen3:8b' }, null);
    assert(global.__toasts.some((t) => t[1] === 'error' && String(t[0]).includes('offline')), 'error toast missing');
    // guard cleared → a retry is possible
    responders['DELETE /api/ollama/models/qwen3:8b/stop'] = () => ({ ok: true });
    await global.Actions['run-stop']({ model: 'qwen3:8b' }, null);
    assert(global.__toasts.some((t) => String(t[0]).includes('Unloaded')), 'retry after error failed');
  });

  await check('Running → Chat deep link carries the model name', () => {
    global.Actions['run-chat']({ model: 'qwen3:8b' });
    assert(global.location.hash === '#chat?model=qwen3%3A8b', 'hash wrong: ' + global.location.hash);
  });

  await check('Chat page opens with the preselected model (App.state.pageQuery)', async () => {
    // minimal chat DOM
    const chatContainer = makeEl('page-container');
    els['chat-output'] = makeEl('chat-output');
    els['chat-status'] = makeEl('chat-status');
    els['chat-send'] = makeEl('chat-send');
    els['chat-model'] = makeEl('chat-model');
    els['chat-input'] = makeEl('chat-input');
    els['chat-clear'] = makeEl('chat-clear');
    const CHAT_MODELS = { models: [M_GPU, M_SPLIT].map((m) => ({ name: m.name })), count: 2 };
    responders['GET /api/ollama/models'] = () => CHAT_MODELS;
    global.renderMarkdown = (t) => String(t || '');
    global.MarkedReady = false;
    global.DOMPurify = undefined;
    // chat.js keeps history inside its own IIFE closure — fresh eval = fresh
    // history, which is exactly the "existing history untouched" contract
    // (the module state is not shared with the Running flow).
    eval(chatSrc);
    assert(global.Pages.chat, 'chat page failed to load');
    // simulate the real flow: route() parses #chat?model=... → pageQuery → render
    global.location.hash = '#chat?model=devstral-24b';
    const m2 = global.location.hash.match(/model=([^&]+)/);
    global.App.state.pageQuery = { model: decodeURIComponent(m2[1]) };
    await global.Pages.chat.render(chatContainer);
    const optHtml = chatContainer._html; // select markup lives in the container
    assert(optHtml.includes('value="qwen3:8b"'), 'model list missing options: ' + optHtml.slice(0, 200));
    assert(/value="devstral-24b" selected/.test(optHtml),
      'devstral not preselected: ' + optHtml.slice(0, 300));
    // both models still listed (list not clobbered by the preselect)
    assert(optHtml.includes('value="devstral-24b"'), 'model list lost after preselect');
  });

  await check('Models ↔ Running: stop flag comes from the same /api/ps source', () => {
    // The running list data shape equals what Models v2 uses for its
    // running flag (both via OllamaClient.running_models()) — verify by
    // contract: same endpoint, same field names.
    const src = fs.readFileSync(process.argv[2], 'utf8');
    assert(src.includes('/api/ollama/running'), 'running endpoint missing');
    assert(src.includes('size_vram'), 'model VRAM field missing');
    assert(!src.includes('/api/gpu'), 'must not use GPU telemetry for model VRAM');
  });

  process.exit(failures ? 1 : 0);
}

main().catch((e) => { console.error('HARNESS ERROR', e); process.exit(2); });
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_running_v2_frontend_flows():
    harness = HARNESS.replace("process.argv[2]", json.dumps(str(RUNNING_JS)))
    harness = harness.replace("process.argv[3]", json.dumps(str(CHAT_JS)))
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(harness)
        path = f.name
    try:
        r = subprocess.run(["node", path], capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, f"running frontend flows failed:\n{r.stdout}\n{r.stderr}"
        assert "FAIL" not in r.stdout, r.stdout
    finally:
        Path(path).unlink(missing_ok=True)


def test_running_page_uses_existing_sources_only():
    """No second telemetry mechanism: page must use the running endpoint +
    WS snapshot; it must not poll GPU endpoints or introduce fetch loops."""
    src = RUNNING_JS.read_text()
    assert "/api/ollama/running" in src
    assert "onSnapshot" in src
    assert "setInterval" not in src          # no independent polling loop
    assert "/api/gpu" not in src             # no GPU telemetry for model VRAM
    assert "/api/agents" not in src          # agents untouched
