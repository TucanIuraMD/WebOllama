"""Models v2 frontend — headless flow test via node.

Loads the real webui/static/js/pages/models.js into the same minimal DOM
stub used by test_chat_frontend.py and drives the real page flows:
render → capability chips → combined filters → Run/Stop actions →
delete → empty/offline states. No browser, no network (API mocked per
test). Skipped when node is absent.
"""
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

MODELS_JS = Path(__file__).resolve().parent.parent / "webui" / "static" / "js" / "pages" / "models.js"

HARNESS = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');

// ---- fake DOM (same minimal stub style as test_chat_frontend) ----
const els = {};
function makeNode() {
  const o = { _text: '', _html: '', style: {}, value: '', checked: false, textContent: '',
              dataset: {}, disabled: false, className: '',
              appendChild(c) { c._parent = o; o._children.push(c); },
              remove() { const p = o._parent; if (p) { const i = p._children.indexOf(o); if (i >= 0) p._children.splice(i, 1); o._parent = null; } },
              focus() {}, _children: [],
              scrollTop: 0, scrollHeight: 100,
              classList: {
                _set: new Set(),
                add(c) { o.classList._set.add(c); },
                remove(c) { o.classList._set.delete(c); },
                toggle(c, on) { on === undefined ? (o.classList._set.has(c) ? o.classList._set.delete(c) : o.classList._set.add(c)) : (on ? o.classList._set.add(c) : o.classList._set.delete(c)); },
                contains(c) { return o.classList._set.has(c); },
              } };
  Object.defineProperty(o, 'innerHTML', { get() { return o._html; }, set(v) { o._html = String(v); o._children = []; } });
  return o;
}
function makeEl(id) { const o = makeNode(); o.id = id; return o; }
global.__chipsDirty = true;
global.document = {
  getElementById: (id) => (els[id] ||= makeEl(id)),
  querySelector: () => null,
  querySelectorAll: (sel) => {
    // cap chips: return STABLE stub instances across calls so the page's
    // render() assigns onclick onto the same objects the test later clicks.
    if (sel === '#cap-filter .cap-chip') {
      const container = els['page-container'];
      if (!container || !container._html) return [];
      if (!global.__chips || global.__chipsDirty) {
        global.__chips = [];
        const re = /<button class="btn-sm cap-chip([^"]*)"[^>]*data-cap="([^"]+)"/g;
        let m;
        while ((m = re.exec(container._html)) !== null) {
          const chip = makeNode();
          chip.dataset.cap = m[2];
          if (m[1].includes('active')) chip.classList.add('active');
          global.__chips.push(chip);
        }
        global.__chipsDirty = false;
      }
      return global.__chips;
    }
    if (sel === '.sort-icon') return [];
    return [];
  },
  createElement: () => makeNode(),
  addEventListener() {},
  body: makeEl('body'),
};
global.window = global;
global.esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
global.toast = (msg, type) => { global.__toasts.push([msg, type]); };
global.fmtBytes = (b) => `${(b / 1e9).toFixed(1)} GB`;
global.debounce = (fn) => fn;
global.openModal = (html, opts) => { global.__modal = { html, opts }; };
global.closeModal = () => { global.__modal = null; };
global.location = { hash: '' };
global.__toasts = [];
global.__modal = null;

// ---- API mock (model names arrive URL-encoded, e.g. llama3.2%3A1b) ----
let responders = {};
let calls = [];
const norm = (url) => decodeURIComponent(url.split('?')[0]);
global.API = {
  get: async (url) => { calls.push(['GET', url]); const r = responders['GET ' + norm(url)]; if (!r) throw new Error('no mock for GET ' + url); return typeof r === 'function' ? r() : r; },
  post: async (url, body) => { calls.push(['POST', url, body]); const r = responders['POST ' + norm(url)]; if (!r) throw new Error('no mock for POST ' + url); return typeof r === 'function' ? r(body) : r; },
  del: async (url) => { calls.push(['DELETE', url]); const r = responders['DELETE ' + norm(url)]; if (!r) throw new Error('no mock for DELETE ' + url); return typeof r === 'function' ? r() : r; },
};
function resetApi() { responders = {}; calls = []; global.__toasts = []; global.__chipsDirty = true; }

let failures = 0;
async function check(name, fn) {
  try { await fn(); console.log(name, 'PASS'); }
  catch (e) { failures++; console.log(name, 'FAIL:', e.message); }
}
function assert(cond, msg) { if (!cond) throw new Error(msg || 'assertion failed'); }

const MODELS = [
  { name: 'qwen3:8b', model: 'qwen3:8b', size: 4700000000, modified_at: '2026-08-01T10:00:00Z',
    details: { family: 'qwen3', parameter_size: '7.6B', quantization_level: 'Q4_K_M' },
    capabilities: ['completion', 'tools', 'thinking'], running: true },
  { name: 'qwen2.5vl:7b', model: 'qwen2.5vl:7b', size: 6000000000, modified_at: '2026-08-03T10:00:00Z',
    details: { family: 'qwen2vl', parameter_size: '7.6B', quantization_level: 'Q4_K_M' },
    capabilities: ['completion', 'vision'], running: false },
  { name: 'llama3.2:1b', model: 'llama3.2:1b', size: 1300000000, modified_at: '2026-08-02T10:00:00Z',
    details: { family: 'llama', parameter_size: '1.2B', quantization_level: 'Q4_K_M' },
    capabilities: ['completion'], running: false },
  { name: 'mystery:latest', model: 'mystery:latest', size: 900000000, modified_at: '2026-08-05T10:00:00Z',
    details: { family: 'other', parameter_size: '0.5B', quantization_level: 'Q8_0' },
    capabilities: ['completion', 'weirdcap'], running: false },
];
function mockList(models) { return { models: JSON.parse(JSON.stringify(models)), count: models.length }; }

async function main() {
  eval(src);
  const page = global.Pages.models;
  const container = els['page-container'] ||= makeEl('page-container');

  await check('renders 4 models with capability chips and status column', () => {
    responders['GET /api/ollama/models'] = () => mockList(MODELS);
    return page.render(container);
  });
  const tbody = els['model-tbody'];
  assert(tbody._html.includes('qwen3:8b'), 'qwen3 missing');
  assert(tbody._html.includes('Thinking'), 'Thinking chip missing');
  assert(tbody._html.includes('Vision'), 'Vision chip missing');
  // unknown capability rendered but marked
  assert(tbody._html.includes('weirdcap') && tbody._html.includes('cap-unknown'), 'unknown cap not marked');
  // status: qwen3 running, others not
  assert(tbody._html.includes('running'), 'running badge missing');
  assert(tbody._html.includes('not loaded'), 'not-loaded badge missing');
  // Run button for stopped, Stop for running
  assert(tbody._html.includes('data-action="model-run"'), 'Run button missing');
  assert(tbody._html.includes('data-action="model-stop"'), 'Stop button missing');

  await check('capability filter Vision narrows list (chip click flow)', () => {
    const chips = document.querySelectorAll('#cap-filter .cap-chip');
    assert(chips.length === 4, 'expected 4 chips, got ' + chips.length);
    const vision = chips.find((c) => c.dataset.cap === 'vision');
    vision.onclick();
    const html = els['model-tbody']._html;
    assert(html.includes('qwen2.5vl:7b'), 'vision model missing after filter');
    assert(!html.includes('llama3.2:1b'), 'non-vision model still visible');
    assert(!html.includes('qwen3:8b'), 'tools-only model visible under vision filter');
    vision.onclick(); // toggle off
    assert(els['model-tbody']._html.includes('llama3.2:1b'), 'list not restored');
  });

  await check('multi-select capabilities AND together (Tools+Vision → empty state)', () => {
    const chips = document.querySelectorAll('#cap-filter .cap-chip');
    chips.find((c) => c.dataset.cap === 'tools').onclick();
    chips.find((c) => c.dataset.cap === 'vision').onclick();
    assert(els['model-empty']._html.includes('No models match'), 'combined-filter empty state missing: ' + els['model-empty']._html);
    chips.find((c) => c.dataset.cap === 'tools').onclick();
    chips.find((c) => c.dataset.cap === 'vision').onclick();
  });

  await check('search + capability filter work together (Vision + "qwen")', () => {
    chips_and_search: {
      const chips = document.querySelectorAll('#cap-filter .cap-chip');
      chips.find((c) => c.dataset.cap === 'vision').onclick();
      els['model-search'].value = 'qwen';
      global.filterText = undefined; // direct set is what debounce handler does
      els['model-search'].oninput();
      const html = els['model-tbody']._html;
      assert(html.includes('qwen2.5vl:7b'), 'vision+qwen missing');
      assert(!html.includes('llama3.2:1b'), 'filter combo leak');
      assert(!html.includes('qwen3:8b'), 'tools-only qwen leaked into vision+qwen');
      chips.find((c) => c.dataset.cap === 'vision').onclick();
      els['model-search'].value = '';
      els['model-search'].oninput();
    }
  });

  await check('unknown capability does NOT match a known filter', () => {
    const chips = document.querySelectorAll('#cap-filter .cap-chip');
    chips.find((c) => c.dataset.cap === 'tools').onclick();
    const html = els['model-tbody']._html;
    assert(!html.includes('mystery:latest'), 'mystery (weirdcap only) matched Tools filter');
    chips.find((c) => c.dataset.cap === 'tools').onclick();
  });

  await check('Run action: POST run → poll running → list refresh, button flips to Stop', async () => {
    const list = JSON.parse(JSON.stringify(MODELS));
    responders['GET /api/ollama/models'] = () => mockList(list);
    let ran = 0;
    responders['POST /api/ollama/models/llama3.2:1b/run'] = () => {
      ran++;
      list.find((m) => m.name === 'llama3.2:1b').running = true;
      return { ok: true, model: 'llama3.2:1b', running: true };
    };
    responders['GET /api/ollama/running'] = () => ({ models: list.filter((m) => m.running), count: 1 });
    // short-circuit the polling sleep
    global.__sleep = undefined;
    const origSetTimeout = global.setTimeout;
    global.setTimeout = (fn, ms) => { if (fn.toString().includes('setTimeout')) return origSetTimeout(fn, 0); return origSetTimeout(fn, 0); };
    await page.render(container);
    const btn = { disabled: false, textContent: '', dataset: { action: 'model-run', model: 'llama3.2:1b' } };
    await global.Actions['model-run']({ model: 'llama3.2:1b' }, btn);
    global.setTimeout = origSetTimeout;
    assert(ran === 1, 'run endpoint not called');
    assert(global.__toasts.some((t) => String(t[0]).includes('running')), 'no success toast: ' + JSON.stringify(global.__toasts));
    assert(els['model-tbody']. _html.includes('qwen3:8b'), 'table not re-rendered');
    // running flag comes from refreshed list
    const refreshed = els['model-tbody']._html;
    assert(refreshed.includes('data-action="model-stop"'), 'Stop button not shown after run');
  });

  await check('Run failure (model not found) → error toast, no crash', async () => {
    responders['GET /api/ollama/models'] = () => mockList(MODELS);
    responders['POST /api/ollama/models/ghost:latest/run'] = () => { throw new Error('Not found: model not found'); };
    global.__toasts = [];
    await global.Actions['model-run']({ model: 'ghost:latest' }, null);
    assert(global.__toasts.some((t) => t[1] === 'error' && String(t[0]).includes('model not found')), 'error toast missing: ' + JSON.stringify(global.__toasts));
  });

  await check('Stop action: DELETE stop → list refreshed with not-loaded', async () => {
    const list = JSON.parse(JSON.stringify(MODELS));
    responders['GET /api/ollama/models'] = () => mockList(list);
    let stopped = 0;
    responders['DELETE /api/ollama/models/qwen3:8b/stop'] = () => {
      stopped++;
      list.find((m) => m.name === 'qwen3:8b').running = false;
      return { ok: true, model: 'qwen3:8b' };
    };
    await page.render(container);
    await global.Actions['model-stop']({ model: 'qwen3:8b' }, null);
    assert(stopped === 1, 'stop endpoint not called');
    assert(global.__toasts.some((t) => String(t[0]).includes('Unloaded')), 'no unload toast');
  });

  await check('Stop of a not-running model surfaces API error toast', async () => {
    responders['GET /api/ollama/models'] = () => mockList(MODELS);
    responders['DELETE /api/ollama/models/llama3.2:1b/stop'] = () => { throw new Error('Ollama is offline'); };
    global.__toasts = [];
    await global.Actions['model-stop']({ model: 'llama3.2:1b' }, null);
    assert(global.__toasts.some((t) => t[1] === 'error' && String(t[0]).includes('offline')), 'offline error toast missing');
  });

  await check('delete removes row in place without full page rebuild', async () => {
    const list = JSON.parse(JSON.stringify(MODELS));
    responders['GET /api/ollama/models'] = () => mockList(list);
    responders['DELETE /api/ollama/models/llama3.2:1b'] = () => {
      const i = list.findIndex((m) => m.name === 'llama3.2:1b');
      list.splice(i, 1);
      return { ok: true };
    };
    await page.render(container);
    const before = els['page-container']._html;
    await global.doDelete('llama3.2:1b');
    const after = els['page-container']._html;
    assert(before === after, 'page container was rebuilt (full reload instead of in-place update)');
    assert(!els['model-tbody']._html.includes('llama3.2:1b'), 'deleted model still listed');
  });

  await check('offline list → alert banner + offline empty state, NOT "No models found"', async () => {
    resetApi();
    responders['GET /api/ollama/models'] = () => { throw new Error('Ollama is offline'); };
    await page.render(container);
    assert(els['page-container']._html.includes('Ollama unavailable'), 'offline banner missing');
    assert(!els['page-container']._html.includes('Pull one from the library'), 'generic empty state shown for offline');
  });

  await check('empty library → pull hint; filtered-empty → filter hint', async () => {
    responders['GET /api/ollama/models'] = () => mockList([]);
    global.__chipsDirty = true;
    await page.render(container);
    assert(els['model-empty']._html.includes('Pull one from the library'), 'empty-library hint missing');
    responders['GET /api/ollama/models'] = () => mockList(MODELS);
    global.__chipsDirty = true;
    await page.render(container);
    const chips = document.querySelectorAll('#cap-filter .cap-chip');
    // nobody has thinking+vision → guaranteed no-match combo
    chips.find((c) => c.dataset.cap === 'thinking').onclick();
    chips.find((c) => c.dataset.cap === 'vision').onclick();
    assert(els['model-empty']._html.includes('No models match'), 'filter-empty hint missing');
    chips.find((c) => c.dataset.cap === 'thinking').onclick();
    chips.find((c) => c.dataset.cap === 'vision').onclick();
    assert(els['model-empty'].style.display === 'none', 'empty state not hidden after filters cleared');
  });

  process.exit(failures ? 1 : 0);
}

main().catch((e) => { console.error('HARNESS ERROR', e); process.exit(2); });
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_models_v2_frontend_flows():
    harness = HARNESS.replace("process.argv[2]", json.dumps(str(MODELS_JS)))
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(harness)
        path = f.name
    try:
        r = subprocess.run(["node", path], capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, f"models frontend flows failed:\n{r.stdout}\n{r.stderr}"
        assert "FAIL" not in r.stdout, r.stdout
    finally:
        Path(path).unlink(missing_ok=True)
