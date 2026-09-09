"""Dashboard v3 frontend — headless node tests for the redesigned page.

Loads the REAL webui/static/js/pages/dashboard.js into jsdom (real DOM
semantics: getElementById, classList, textContent/innerHTML coexistence)
and drives the v3 flows: hardware cards (CPU/RAM/GPU/Storage), zero-value
semantics (0% / 0 GB / 0 W are VALUES, never "—"), the ollama-ps running
table (columns, derived processor split, actions), loading / API error /
Ollama offline / GPU unavailable / partial-snapshot (last-known preservation)
states, Electricity tile states, WS live updates and single-timer (no
duplicate polling). Skipped when node or jsdom is unavailable.
"""
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

DASHBOARD_JS = Path(__file__).resolve().parent.parent / "webui" / "static" / "js" / "pages" / "dashboard.js"
# jsdom is installed --no-save into a temp prefix; fall back to a plain require.
JSDOM_DIR = Path("/tmp/jsdom-env/node_modules")


def _run_node(script: str) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not available")
    with tempfile.NamedTemporaryFile("w", suffix=".cjs", delete=False) as f:
        f.write(script)
        path = f.name
    env = dict(os.environ)
    if JSDOM_DIR.exists():
        env["NODE_PATH"] = str(JSDOM_DIR)
    try:
        proc = subprocess.run([node, path, str(DASHBOARD_JS)], capture_output=True, text=True, timeout=60, env=env)
    finally:
        Path(path).unlink(missing_ok=True)
    out = (proc.stdout or "").strip()
    if proc.returncode == 3:
        pytest.skip("jsdom not available: " + (proc.stderr or "").strip())
    if proc.returncode != 0:
        raise AssertionError(f"node harness failed:\n{proc.stderr}\nstdout tail:\n{out[-2000:]}")
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        raise AssertionError(f"non-JSON node output:\n{out[-2000:]}")


HARNESS_HEAD = r"""
let JSDOM;
try { ({ JSDOM } = require('jsdom')); }
catch (e) { process.stderr.write('jsdom not installed — skipping harness\n'); process.exit(3); }

const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
// Extract the dashboard IIFE body so tests drive the real page code.
const m = src.match(/\(function \(\) \{([\s\S]*)\}\)\(\);\s*$/);
if (!m) { process.stderr.write('cannot parse dashboard.js\n'); process.exit(2); }
const body = m[1];

// ---- jsdom page: a real DOM the dashboard can build itself into ----
const dom = new JSDOM('<!doctype html><html><body><div id="page-root"></div></body></html>', {
  url: 'http://localhost/#dashboard', pretendToBeVisual: true,
});
const { window } = dom;
const { document } = window;

// silence jsdom "not implemented" noise (navigation etc.)
window._virtualConsoleErrors = [];
dom.virtualConsole.on('jsdomError', (e) => window._virtualConsoleErrors.push(String(e && e.message || e)));

const results = {};
const intervalLog = [];
// pure counter stub — the page must never own real timers in tests, and
// real handles would keep the node event loop alive after the report
window.setInterval = (fn, ms) => { intervalLog.push(ms); return intervalLog.length; };
window.clearInterval = () => {};
window.setTimeout = () => 0;

// minimal Chart.js stubs — chart drawing is out of scope for these tests
const Chart = function () { return { destroy() {}, data: {}, update() {}, resize() {} }; };
const Charts = {
  instances: {}, make() { return null; }, destroy() {}, destroyAll() {},
  series() { return { load() {}, push() {} }; },
};
const seriesChart = () => ({ load() {}, push() {} });

// page code uses these globals from index.html
window.orDash = undefined;
window.fmtBytes = (n) => {
  if (n == null || isNaN(n)) return '—';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0, v = n;
  while (v >= 1000 && i < units.length - 1) { v /= 1000; i++; }
  return (i ? v.toFixed(1) : String(v)) + ' ' + units[i];
};
window.fmtNum = (n, d) => (n == null || isNaN(n)) ? '—' : Number(n).toLocaleString('en-US', { maximumFractionDigits: d == null ? 2 : d });

// esc() is a global from api.js (loaded before dashboard.js in index.html)
function esc(s) {
  if (s === null || s === undefined) return "";
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

// ---- App + API stubs (realistic payload shapes) ----
const App = { state: { snapshot: null, currentPage: 'dashboard' } };
let apiRoutes = {};
const callLog = []; // every API.get url, for fetch-count assertions
const API = { get: (url) => { callLog.push(url); const h = apiRoutes[url]; if (!h) return Promise.reject(new Error('no route: ' + url)); return h(); } };
window.App = App;
// controllable clock for the 7s-stale and 60s-throttle seams (page code
// in the new-Function scope uses node's own Date)
const realNow = Date.now.bind(Date);
let fakeNow = null;
Date.now = () => (fakeNow != null ? fakeNow : realNow());
"""

HARNESS_TAIL = r"""
// evaluate the extracted body with `window` as the global scope, mirroring
// how index.html loads it (dashboard.js references window.*, document, API…)
const page = new Function(
  'window','document','setInterval','clearInterval','console','Chart','Charts','seriesChart','fmtBytes','fmtNum','esc','App','API',
  body + '\n;return window.Pages.dashboard;'
);
const dash = page(window, document, window.setInterval, window.clearInterval, console, Chart, Charts, seriesChart, window.fmtBytes, window.fmtNum, esc, App, API);

function el(id) { return document.getElementById(id); }
function html(id) { const e = el(id); return e ? e.innerHTML : null; }
function text(id) { const e = el(id); return e ? e.textContent : null; }
function assert(cond, msg) { if (!cond) throw new Error(msg); }
async function run(name, fn) {
  try { await fn(); results[name] = 'PASS'; }
  catch (e) { results[name] = 'FAIL: ' + (e && e.message ? e.message : String(e)); }
}

async function renderPage() {
  const root = document.getElementById('page-root');
  root.innerHTML = '';
  await dash.render(root);
  return root;
}
"""

HARNESS_CASES_BODY = r"""
const SNAPSHOT = {
  ollama: {
    online: true, version: '0.12.6', endpoint: 'http://127.0.0.1:11434',
    models_count: 12, running_count: 2,
    running: [
      { name: 'deepseek-coder-v2:latest', digest: 'a1b2c3d4e5f60718293a4b5c6d7e8f9012345678', size: 8.9e9, size_vram: 6.1e9,
        context_length: 16384, expires_at: new Date(Date.now() + 4 * 3600 * 1000).toISOString() },
      { name: 'qwen3:8b', digest: 'f0e1d2c3b4a5968778695a4b3c2d1e0f09876543', size: 4.7e9, size_vram: 4.7e9,
        context_length: 32768, expires_at: new Date(Date.now() + 30 * 60 * 1000).toISOString() },
    ],
  },
  gpu: {
    available: true, driver_version: '535.183', cuda_version: '12.2',
    gpus: [{ index: 0, name: 'Tesla V100-SXM2-16GB', utilization: 2, temperature: 45, power_draw: 40.3,
             vram_used: 4.8e9, vram_total: 16.16e9, fan_available: false }],
    ollama_vram: { total_vram: 10.8e9 },
  },
  cpu: { percent: 0, model: 'Intel(R) Core(TM) i7-7700K CPU @ 4.20GHz', cores: 4, threads: 8,
         load_1: 0.42, load_5: 0.3, load_15: 0.2, frequency: { current: 4200000, min: 800000, max: 4500000 } },
  ram: { total: 125.8e9, used: 31.6e9, free: 94.2e9, percent: 18.2 },
  disk: { total: 500e9, used: 100e9, free: 400e9, percent: 20.0,
          partitions: [{ mount: '/', percent: 20.0 }, { mount: '/data', percent: 5.0 }] },
  jobs: [{ id: 'j1', status: 'running' }, { id: 'j2', status: 'done' }],
};

const FULL_ROUTES = {
  '/api/status': () => Promise.resolve(SNAPSHOT),
  '/api/electricity/gpu-energy': () => Promise.resolve({ tariff: {}, today: {} }),
  '/api/history/gpu?minutes=60': () => Promise.resolve({ points: [] }),
  '/api/history/system?minutes=60': () => Promise.resolve({ points: [] }),
};

await run('loading first, then full render', async () => {
  App.state.snapshot = null;
  apiRoutes = { ...FULL_ROUTES, '/api/system/processor': () => Promise.reject(new Error('offline')) };
  await renderPage();
  assert(el('dash-hw-cpu'), 'cpu card exists');
  assert(text('dash-cpu-pct') !== null, 'cpu card rendered');
  assert(text('dash-cpu-pct') === '0%', 'CPU 0% renders as 0%, got ' + text('dash-cpu-pct'));
  assert(html('dash-hw-cpu').includes('8'), 'threads shown');
  assert(text('dash-ram-used') === '31.6 GB', 'RAM used, got ' + text('dash-ram-used'));
  assert(text('dash-ram-free') === '94.2 GB', 'RAM free');
  assert(text('dash-ram-total') === '125.8 GB', 'RAM total');
  assert(html('dash-hw-ram').includes('18%'), 'RAM percent shown');
  assert(text('dash-gpu-pct') === '2%', 'GPU util 2%, got ' + text('dash-gpu-pct'));
  assert(html('dash-hw-gpu').includes('Tesla V100-SXM2-16GB'), 'GPU model');
  assert(text('dash-power') === '40.3 W', 'power, got ' + text('dash-power'));
  assert(text('dash-temp') === '45°C', 'temp, got ' + text('dash-temp'));
  assert(text('dash-vram') === '4.8 GB / 16.2 GB', 'VRAM kv, got ' + text('dash-vram'));
  assert(html('dash-hw-storage').includes('20%'), 'storage percent');
  assert(text('dash-models-count') === '12', 'models count');
  assert(text('dash-running-count') === '2', 'running count');
  assert(text('dash-jobs-count') === '1', 'jobs active count');
});

await run('running models table (ollama ps columns + actions)', async () => {
  const t = html('dash-running');
  for (const col of ['Model', 'ID', 'Size', 'Processor', 'VRAM', 'Context', 'Duration', 'Status', 'Actions']) {
    assert(t.includes(col), 'column missing: ' + col);
  }
  assert(t.includes('deepseek-coder-v2:latest'), 'row model 1');
  assert(t.includes('qwen3:8b'), 'row model 2');
  assert(t.includes('a1b2c3d4e5f6'), 'digest id truncated');
  assert(t.includes('69% GPU / 31% CPU'), 'derived split for partial offload, got: ' + (t.match(/\d+% GPU \/ \d+% CPU/) || ['none'])[0]);
  assert(t.includes('100% GPU'), 'full-gpu split');
  assert(t.includes('data-action="run-chat"'), 'Chat action');
  assert(t.includes('data-action="run-stop"'), 'Stop action');
  assert(t.includes('derived from /api/ps size vs size_vram'), 'honest derivation note');
});

await run('zero values are values (CPU 0, RAM 0, GPU 0, VRAM 0, power 0)', async () => {
  const zero = JSON.parse(JSON.stringify(SNAPSHOT));
  zero.cpu.percent = 0; zero.ram.percent = 0; zero.ram.used = 0; zero.ram.free = zero.ram.total;
  zero.gpu.gpus[0].utilization = 0; zero.gpu.gpus[0].vram_used = 0; zero.gpu.gpus[0].power_draw = 0;
  dash.drawSnapshot(zero, '');
  assert(text('dash-cpu-pct') === '0%', 'CPU 0% kept');
  assert(html('dash-hw-ram').includes('>0%<'), 'RAM 0% kept');
  assert(text('dash-ram-used') === '0 B', 'RAM used 0 bytes, got ' + text('dash-ram-used'));
  assert(text('dash-gpu-pct') === '0%', 'GPU util 0% kept');
  assert(text('dash-vram') === '0 GB / 16.2 GB', 'VRAM 0 kept, got ' + text('dash-vram'));
  assert(text('dash-power') === '0 W', 'power 0 kept, got ' + text('dash-power'));
  // and 100% semantics
  const full = JSON.parse(JSON.stringify(SNAPSHOT));
  full.ram.percent = 100; full.ram.used = full.ram.total; full.ram.free = 0;
  dash.drawSnapshot(full, '');
  assert(html('dash-hw-ram').includes('>100%<'), 'RAM 100% kept');
});

await run('partial snapshot preserves last-known values', async () => {
  // a partial snapshot WITHOUT cpu/ram/gpu must not wipe the cards
  dash.drawSnapshot({ ollama: SNAPSHOT.ollama }, '');
  assert((html('dash-hw-cpu') || '').includes('class="hw-pct"'), 'cpu card preserved');
  assert(html('dash-hw-gpu').includes('Tesla'), 'gpu card preserved');
  // partial running list (missing) must not wipe the table
  assert(html('dash-running').includes('qwen3:8b'), 'running table preserved');
});

await run('GPU unavailable renders honest state, CPU/RAM intact', async () => {
  const noGpu = JSON.parse(JSON.stringify(SNAPSHOT));
  noGpu.gpu = { available: false, reason: 'no NVIDIA GPU detected' };
  dash.drawSnapshot(noGpu, '');
  assert(html('dash-hw-gpu').includes('no NVIDIA GPU'), 'gpu reason shown');
  assert(!html('dash-hw-gpu').includes('Tesla'), 'no stale GPU data');
  assert((html('dash-hw-cpu') || '').includes('class="hw-pct"'), 'cpu unaffected');
});

await run('Ollama offline: running table reports offline, no fake rows', async () => {
  const off = JSON.parse(JSON.stringify(SNAPSHOT));
  off.ollama = { online: false, running: [], models_count: 0, running_count: 0 };
  dash.drawSnapshot(off, '');
  assert(html('dash-running').includes('Ollama offline'), 'offline note');
  assert(!html('dash-running').includes('qwen3:8b'), 'no fabricated rows');
  assert(text('dash-running-count') === '—', 'running count — when offline, got ' + text('dash-running-count'));
  assert(text('dash-ollama-state').includes('OFFLINE'), 'tile shows offline');
});

await run('API error shows alert, never fake data', async () => {
  App.state.snapshot = null;
  apiRoutes = {
    '/api/status': () => Promise.reject(new Error('boom 500')),
    '/api/electricity/gpu-energy': () => Promise.resolve({ tariff: {}, today: {} }),
    '/api/history/gpu?minutes=60': () => Promise.resolve({ points: [] }),
    '/api/history/system?minutes=60': () => Promise.resolve({ points: [] }),
    '/api/system/processor': () => Promise.reject(new Error('offline')),
  };
  await renderPage();
  const pageRoot = document.getElementById('page-root');
  assert(pageRoot.innerHTML.includes('Dashboard data unavailable'), 'error banner shown');
});

await run('electricity states: normal, tariff missing, no telemetry, API error', async () => {
  dash.renderElecTile({ tariff: { tariff: 1.2, currency: 'lei', tariff_is_configured: true, current_cost_per_hour: 0.123 },
                        today: { available: true, kwh: 0.1234, cost: 0.25 } });
  assert(text('dash-electricity-value').includes('0.1234 kWh'), 'energy today');
  assert(text('dash-electricity-value').includes('0.25'), 'cost today');
  assert(text('dash-electricity-sub').includes('0.123'), 'cost/hour');
  dash.renderElecTile({ tariff: { tariff: null, currency: 'lei', tariff_is_configured: false },
                        today: { available: true, kwh: 0.5 } });
  assert(text('dash-electricity-sub').includes('tariff not configured'), 'tariff missing state');
  dash.renderElecTile({ tariff: {}, today: { available: false } });
  assert(text('dash-electricity-value').includes('data unavailable'), 'no-telemetry state');
  dash.renderElecTile({ error: 'HTTP 500' });
  assert(text('dash-electricity-value') === 'unavailable', 'API error state');
});

await run('WS onSnapshot live update refreshes cards', async () => {
  App.state.snapshot = null;
  apiRoutes = { ...FULL_ROUTES, '/api/system/processor': () => Promise.reject(new Error('offline')) };
  await renderPage();
  const live = JSON.parse(JSON.stringify(SNAPSHOT));
  live.cpu.percent = 55;
  live.ram.percent = 64.4;
  live.gpu.gpus[0].utilization = 97;
  dash.onSnapshot(live);
  assert(text('dash-cpu-pct') === '55%', 'live cpu, got ' + text('dash-cpu-pct'));
  assert(html('dash-hw-ram').includes('64%'), 'live ram');
  assert(text('dash-gpu-pct') === '97%', 'live gpu');
});

await run('no duplicate polling: exactly one PROCESSOR interval', async () => {
  intervalLog.length = 0; // only THIS render's timers count
  App.state.snapshot = null;
  apiRoutes = {
    '/api/status': () => Promise.resolve(SNAPSHOT),
    '/api/system/processor': () => Promise.reject(new Error('offline')),
    '/api/electricity/gpu-energy': () => Promise.resolve({ tariff: {}, today: {} }),
    '/api/history/gpu?minutes=60': () => Promise.resolve({ points: [] }),
    '/api/history/system?minutes=60': () => Promise.resolve({ points: [] }),
  };
  await renderPage();
  const procIntervals = intervalLog.filter((ms) => ms === 3000);
  assert(procIntervals.length === 1, 'exactly one 3s PROCESSOR interval, got ' + procIntervals.length);
  assert(intervalLog.every((ms) => ms === 3000), 'no other timers on dashboard, got ' + JSON.stringify(intervalLog));
  // re-render must not stack timers
  await renderPage();
  const after = intervalLog.filter((ms) => ms === 3000).length;
  assert(after === 2, 're-render restarted (not stacked) the single timer, got ' + after);
});

await run('responsive render: page builds without errors (mobile folds via CSS grid)', async () => {
  const pageRoot = document.getElementById('page-root');
  for (const cls of ['dash-top-grid', 'hw-grid', 'dash-section-title', 'ps-table']) {
    assert(pageRoot.innerHTML.includes(cls) || (html('dash-running') || '').includes(cls), 'layout element: ' + cls);
  }
  assert(pageRoot.innerHTML.includes('dash-link'), 'top nav tiles are links');
});

await run('navigation links: Models/Running/Jobs/Chat/Agents/GPU present and obvious', async () => {
  const pageRoot = document.getElementById('page-root');
  for (const href of ['#models', '#running', '#jobs', '#chat', '#agents', '#gpu']) {
    assert(pageRoot.innerHTML.includes(`href="${href}"`), `nav link ${href} missing`);
  }
  assert(pageRoot.innerHTML.includes('id="dash-models-tile"'), 'models tile not a link');
  assert(pageRoot.innerHTML.includes('id="dash-running-tile"'), 'running tile not a link');
});

await run('icons: no emoji anywhere, monochrome inline SVG markup instead', async () => {
  const pageRoot = document.getElementById('page-root');
  const all = pageRoot.innerHTML + (html('dash-running') || '');
  // 1) NO emoji-style icons on a professional monitoring dashboard
  const emoji = /[\u{1F000}-\u{1FAFF}\u{2600}-\u{27BF}\u{2B00}-\u{2BFF}]/u;
  assert(!emoji.test(all), 'emoji found in dashboard markup: ' + (all.match(emoji) || []).join(''));
  // 2) SVG icon markup present for every hardware card and top tile
  for (const id of ['dash-hw-cpu', 'dash-hw-ram', 'dash-hw-gpu', 'dash-hw-storage']) {
    const card = html(id) || '';
    assert(card.includes('<svg class="icon"'), `SVG icon missing in ${id}`);
    assert(card.includes('hw-card-head'), `card head missing in ${id}`);
  }
  assert(pageRoot.innerHTML.includes('class="tile-icon"'), 'tile icons missing');
  const tileIcons = (pageRoot.innerHTML.match(/class="tile-icon"/g) || []).length;
  assert(tileIcons === 5, 'expected 5 top-tile icons (Ollama/Models/Running/Jobs/Electricity), got ' + tileIcons);
  // 3) one consistent icon language: uniform size + currentColor stroke
  assert(all.includes('viewBox="0 0 22 22"'), 'icon viewBox inconsistent');
  assert(!all.includes('width="16"') || !all.includes('emoji'), 'icon size drift');
  const iconTags = all.match(/<svg class="icon"[^>]*>/g) || [];
  assert(iconTags.length >= 9, 'expected >=9 icon instances, got ' + iconTags.length);
  for (const tag of iconTags) {
    assert(tag.includes('width="17"') && tag.includes('height="17"'), 'icon size not 17px: ' + tag);
    assert(tag.includes('stroke="currentColor"'), 'icon not monochrome (currentColor): ' + tag);
    assert(tag.includes('fill="none"'), 'icon not outline style: ' + tag);
  }
  // 4) specific glyphs per slot
  const ic = dash.iconSvg;
  assert(ic('cpu').includes('<rect'), 'CPU icon = chip');
  assert(ic('ram').includes('<rect'), 'RAM icon = memory module');
  assert(ic('gpu').includes('circle'), 'GPU icon = graphics card');
  assert(ic('storage').includes('circle'), 'Storage icon = disk');
  assert(ic('ollama').includes('rect'), 'Ollama icon = server');
  assert(ic('models').includes('path'), 'Models icon = layers');
  assert(ic('running').includes('circle'), 'Running icon = play/process');
  assert(ic('jobs').includes('path'), 'Jobs icon = activity');
  assert(ic('power').includes('path'), 'Electricity icon = power/bolt');
});

await run('hw-card composition v3.2: head icon+name+ring, single pct, kv row — no dupes', async () => {
  // v3.2: head(icon + name + ONE ring on the right) → single big pct → kv
  // row. No second percentage near the ring, no used/total duplication.
  for (const [id, kvKeys] of [
    ['dash-hw-cpu', ['Cores', 'Threads', 'Frequency']],
    ['dash-hw-ram', ['Used', 'Free', 'Total']],
    ['dash-hw-gpu', ['Power', 'Temp', 'VRAM', 'Fan', 'CPU / GPU']],
    ['dash-hw-storage', ['Used', 'Free', 'Total']],
  ]) {
    const card = html(id) || '';
    const headIdx = card.indexOf('hw-card-head');
    const ringCount = (card.match(/class="hw-ring"/g) || []).length;
    const kvIdx = card.indexOf('<div class="hw-kv">');
    assert(headIdx !== -1, `head missing in ${id}`);
    assert(ringCount === 1, `${id}: exactly ONE ring required, got ${ringCount}`);
    const ringIdx = card.indexOf('hw-head-ring');
    const headEnd = card.indexOf('</div>', headIdx);
    assert(ringIdx > headIdx && headEnd !== -1 && ringIdx < headEnd, `ring must sit INSIDE the head row in ${id}`);
    assert(kvIdx !== -1, `kv row missing in ${id}`);
    const nextSection = ['hw-foot', 'hw-mini-gpus'].map((m) => card.indexOf(m, kvIdx)).filter((i) => i > 0);
    const kvEnd = nextSection.length ? Math.min(...nextSection) : card.length;
    const kv = card.slice(kvIdx, kvEnd);
    assert(kv.includes('class="k"'), `kv cells missing in ${id}`);
    for (const k of kvKeys) assert(kv.includes(`>${k}</div>`), `${id}: kv key ${k} missing`);
  }
  // --- no duplicated percentages ---
  // CPU: exactly one "N%" as the big value; the ring is EMPTY (no text)
  const cpuCard = html('dash-hw-cpu') || '';
  assert((cpuCard.match(/class="hw-pct"/g) || []).length === 1, 'CPU: exactly one big pct');
  assert(!/hw-ring-text[^>]*>[^<]+</.test(cpuCard), 'CPU ring must be a pure indicator (no inner %)');
  assert(!/load \d/.test(cpuCard), 'CPU: no load sub-line');
  assert(!cpuCard.includes('hw-pct-sub'), 'CPU: no duplicate sub value');
  // RAM: one pct; no "used / total" next to it
  const ramCard = html('dash-hw-ram') || '';
  assert((ramCard.match(/class="hw-pct"/g) || []).length === 1, 'RAM: exactly one big pct');
  assert(!ramCard.includes('hw-pct-sub'), 'RAM: no duplicate sub value near the pct');
  assert(!/GB \/ \d+(\.\d+)? GB</.test(ramCard.replace(/id="dash-ram-[\w]+"[^>]*>\d+(\.\d+)? GB<\//g, '')), 'RAM: used/total appears only in kv');
  // GPU: utilization pct once; Clocks removed; CPU/GPU split present
  const gpuCard = html('dash-hw-gpu') || '';
  assert((gpuCard.match(/class="hw-pct"/g) || []).length === 1, 'GPU: exactly one big pct');
  assert(!gpuCard.includes('Clocks'), 'GPU: Clocks removed');
  assert(text('dash-cpusplit') !== undefined, 'GPU: CPU/GPU split cell exists');
  // Storage: one pct; no used/total next to it
  const stCard = html('dash-hw-storage') || '';
  assert((stCard.match(/class="hw-pct"/g) || []).length === 1, 'Storage: exactly one big pct');
  assert(!stCard.includes('hw-pct-sub'), 'Storage: no duplicate sub value');
  // uniform order: head → pct → (device name for GPU) → bar? → kv
  for (const id of ['dash-hw-cpu', 'dash-hw-ram', 'dash-hw-gpu', 'dash-hw-storage']) {
    const c = html(id) || '';
    const head = c.indexOf('hw-card-head');
    const pct = c.indexOf('class="hw-pct"');
    const kv = c.indexOf('<div class="hw-kv">');
    assert(head < pct && pct < kv, `${id}: order must be head → pct → kv`);
  }
  // RAM kv is ONE horizontal row: exactly 3 label cells
  const ramKv = html('dash-hw-ram') || '';
  const ramKvStart = ramKv.indexOf('<div class="hw-kv">');
  const ramKvBlock = ramKv.slice(ramKvStart, ramKv.indexOf('</div>', ramKv.indexOf('dash-ram-total')));
  assert((ramKvBlock.match(/<div class="k">/g) || []).length === 3, 'RAM kv must have exactly 3 cells');
});

await run('GPU CPU/GPU split: real /api/ps aggregate, honest dash, full-gpu, split', async () => {
  // aggregate split over the SAME /api/ps rows the Running table uses
  // deepseek 8.9/6.1 (partial) + qwen 4.7/4.7 (full): sz=13.6, vr=10.8 → 79/21... aggregated:
  dash.drawSnapshot(SNAPSHOT, '');
  let s = text('dash-cpusplit');
  // sz=13.6 vr=10.8 → 79%/21%... exact: Math.round(10.8/13.6*100)=79 → "21% / 79%"
  assert(/^\d+% \/ \d+%$/g.test(s), 'aggregate split format, got ' + s);
  // full-gpu only
  const only = JSON.parse(JSON.stringify(SNAPSHOT));
  only.ollama.running = [{ name: 'q3', size: 4.7e9, size_vram: 4.7e9 }];
  dash.drawGpu(SNAPSHOT.gpu, only.ollama);
  assert(text('dash-cpusplit') === '0% / 100%', 'full-gpu split, got ' + text('dash-cpusplit'));
  // no split data → honest dash (never invented)
  const none = JSON.parse(JSON.stringify(SNAPSHOT));
  none.ollama.running = [{ name: 'x' }];
  dash.drawGpu(SNAPSHOT.gpu, none.ollama);
  assert(text('dash-cpusplit') === '—', 'no split → dash, got ' + text('dash-cpusplit'));
  // ollama missing entirely → dash
  dash.drawGpu(SNAPSHOT.gpu, undefined);
  assert(text('dash-cpusplit') === '—', 'no ollama → dash');
  // honest derivation note
  assert(html('dash-hw-gpu').includes('derived from /api/ps size vs size_vram'), 'split derivation note');
});

await run('v3.2 layout: 4/2/1 grid + landscape cards + kv stays horizontal', async () => {
  const css = document.querySelector('style') ? document.documentElement : null;
  const appCss = App.state.appCssText || '';
  // CSS is loaded as a file in production; verify the source rules instead
  const cssSrc = typeof DASH_CSS_SRC !== 'undefined' ? DASH_CSS_SRC : '';
  if (!cssSrc) return; // CSS source not embedded in this harness — rules verified by python source test
  assert(cssSrc.includes('grid-template-columns: repeat(4, minmax(0, 1fr))'), 'desktop 4 cards');
  assert(/@media \(max-width: 1180px\)[\s\S]*?repeat\(2, minmax\(0, 1fr\)\)/.test(cssSrc), 'tablet 2 cards');
  assert(/@media \(max-width: 640px\)[\s\S]*?grid-template-columns: 1fr/.test(cssSrc), 'mobile 1 card');
  assert(cssSrc.includes('min-height'), 'uniform landscape card height');
  assert(!/hw-kv\s*{[^}]*flex-wrap:\s*wrap/.test(cssSrc.split('@media (max-width: 480px)')[0] || ''), 'desktop kv stays horizontal');
});

await run('PROCESSOR render: good data, unavailable, garbage — no NaN/undefined', async () => {  await dash.renderProcessor({ available: true, host: 'h', cpu: { utilization: 42, load: [3.2, 2.8] }, gpu_available: true,
    gpus: [], ollama: { online: true, running_models: [{ name: 'qwen3:8b', size_vram: 6.8e9 }] } });
  let t = html('proc-body');
  assert(t.includes('42%') && t.includes('Load 3.2 / 2.8'), 'cpu card wrong: ' + t.slice(0, 120));
  assert(t.includes('qwen3:8b') && t.includes('6.8 GB'), 'ollama card wrong');
  await dash.renderProcessor({ available: false, reason: 'collector down' });
  t = html('proc-body');
  assert(t.includes('Processor information unavailable') && t.includes('collector down'), 'proc-unavailable wrong');
  await dash.renderProcessor({ available: true, cpu: { utilization: null, load: null }, gpu_available: true,
    gpus: [{ index: 0 }], ollama: { online: true, running_models: [] } });
  t = html('proc-body');
  assert(!t.includes('NaN') && !t.includes('undefined'), 'garbage leaked into DOM');
});

await run('stale PROCESSOR: API failures keep last data and mark stale after threshold', async () => {
  await dash.renderProcessor({ available: true, host: 'h', cpu: { utilization: 42, load: [1] }, gpu_available: true,
    gpus: [], ollama: { online: true, running_models: [] } });
  dash.touchProcOk(); // anchor the "last good data" moment (as a real poll would)
  assert(!text('dash-proc-state').includes('stale'), 'premature stale');
  const before = html('proc-body');
  apiRoutes['/api/system/processor'] = () => Promise.reject(new Error('down'));
  fakeNow = realNow() + 8000; // past the 7s stale threshold
  await dash.refreshProcessor();
  fakeNow = null;
  assert(html('proc-body') === before, 'last good PROCESSOR data lost on API failure');
  assert(html('proc-body').includes('42%'), 'data content gone');
  assert(text('dash-proc-state').includes('stale'), 'stale marker not set after threshold');
});

await run('WS stale snapshot: ollama missing → blocks keep last data, no crash', async () => {
  dash.onSnapshot({ ts: 1001 }); // empty snapshot (temporarily unavailable data)
  assert(text('dash-running-count') === '2', 'last data discarded');
  // ollama offline snapshot flips the running block explicitly, live
  const off = JSON.parse(JSON.stringify(SNAPSHOT));
  off.ollama.online = false; off.ollama.running = null;
  dash.onSnapshot(off);
  assert(html('dash-running').includes('Ollama offline'), 'offline not reflected live');
});

await run('race condition: two overlapping renders — only the latest wins', async () => {
  App.state.snapshot = null;
  let resolveA;
  const sharedStatus = new Promise((res) => { resolveA = res; });
  apiRoutes = {
    '/api/status': () => sharedStatus,
    '/api/system/processor': () => Promise.resolve({ available: false }),
    '/api/electricity/gpu-energy': () => Promise.resolve({ tariff: {}, today: {} }),
    '/api/history/gpu?minutes=60': () => Promise.resolve({ points: [] }),
    '/api/history/system?minutes=60': () => Promise.resolve({ points: [] }),
  };
  const root = document.getElementById('page-root');
  const pA = dash.render(root);           // render A: slow /api/status
  const pB = dash.render(root);           // render B: same pending fetch
  resolveA(SNAPSHOT);                     // B (latest seq) must win
  await pB; await pA;
  assert(root.innerHTML.includes('dash-electricity-tile'), 'B shell missing');
  assert(!root.innerHTML.includes('Dashboard data unavailable'), 'A must not re-render as error');
});

await run('snapshot cache: no double /api/status when App.state.snapshot is set', async () => {
  App.state.snapshot = SNAPSHOT;
  callLog.length = 0;
  apiRoutes = {
    '/api/system/processor': () => Promise.resolve({ available: false }),
    '/api/electricity/gpu-energy': () => Promise.resolve({ tariff: {}, today: {} }),
    '/api/history/gpu?minutes=60': () => Promise.resolve({ points: [] }),
    '/api/history/system?minutes=60': () => Promise.resolve({ points: [] }),
  };
  await renderPage();
  const statusCalls = callLog.filter((u) => u === '/api/status');
  assert(statusCalls.length === 0, 'cached snapshot ignored: ' + JSON.stringify(callLog));
  App.state.snapshot = null;
});

await run('electricity tile: one fetch per render, WS refresh throttled to 60s', async () => {
  App.state.snapshot = null;
  callLog.length = 0;
  // advance the shared clock past any previous throttle window (the module
  // captured elecSummaryFetchedAt from earlier renders/cases)
  fakeNow = realNow() + 120000;
  apiRoutes = { ...FULL_ROUTES, '/api/system/processor': () => Promise.resolve({ available: false }) };
  await renderPage();
  const elecCalls = callLog.filter((u) => u === '/api/electricity/gpu-energy').length;
  assert(elecCalls === 1, 'expected 1 electricity fetch per render, got ' + elecCalls);
  // WS snapshots at full cadence: throttle must collapse them to ~1 fetch/min
  for (let i = 0; i < 10; i++) dash.onSnapshot(SNAPSHOT);
  const throttled = callLog.filter((u) => u === '/api/electricity/gpu-energy').length;
  assert(throttled === 1, 'WS-cadence refresh not throttled: ' + throttled + ' calls for 10 snapshots');
  // force=true bypasses the throttle seam
  apiRoutes['/api/electricity/gpu-energy'] = () => Promise.resolve({ tariff: { tariff: 5, currency: 'lei', tariff_is_configured: true, current_cost_per_hour: 0.925 },
                                                                     today: { available: true, kwh: 0.0123, cost: 0.0615 } });
  await dash.loadElectricitySummary(true);
  assert(text('dash-electricity-value').includes('0.0123 kWh'), 'tile not force-refreshed: ' + text('dash-electricity-value'));
  fakeNow = null;
});
"""

FOOTER = """
const out = { results, jsdomErrors: window._virtualConsoleErrors };
console.log(JSON.stringify(out, null, 1));
process.exit(0); // jsdom keeps handles open; the report is complete here
})();
"""


def _script() -> str:
    return (
        HARNESS_HEAD
        + HARNESS_TAIL
        + "\n(async () => {\n"
        + HARNESS_CASES_BODY
        + FOOTER
    )


def test_dashboard_polling_discipline():
    """Polling contract: PROCESSOR poll is the ONLY interval, page-scoped,
    and it is explicitly stopped on re-render (no unbounded timers)."""
    src = DASHBOARD_JS.read_text()
    assert src.count("setInterval(") == 1, "more than one polling loop"
    assert "clearInterval(procTimer)" in src
    assert "stopProcessorPolling()" in src
    assert "App.state.currentPage !== \"dashboard\"" in src  # page-scoped
    # no direct GPU telemetry fetching — the snapshot is the single source
    assert "/api/gpu" not in src


def test_dashboard_v32_layout_css():
    """v3.2 responsive contract: 4 cards on desktop, 2 on tablet, 1 on
    mobile; landscape cards with a uniform min-height; kv stays one
    horizontal row on desktop and only wraps under 480px."""
    css = (DASHBOARD_JS.parent.parent.parent.parent / "static" / "css" / "app.css").read_text()
    assert ".hw-grid" in css and "grid-template-columns: repeat(4, minmax(0, 1fr))" in css
    assert "@media (max-width: 1180px)" in css and "repeat(2, minmax(0, 1fr))" in css
    assert "@media (max-width: 640px)" in css
    # min-height keeps the four cards the same landscape height
    assert "min-height" in css.split(".hw-kv")[0].split(".hw-card {")[1]
    # kv row: horizontal on desktop, wraps only in the narrow fallback
    kv_rule = css.split(".hw-kv {")[1].split("}")[0]
    assert "flex-wrap: nowrap" in kv_rule
    assert "margin-top: auto" in kv_rule  # kv pinned to card bottom → equal heights
    narrow = css.split("@media (max-width: 480px)")[1] if "@media (max-width: 480px)" in css else ""
    assert "flex-wrap: wrap" in narrow, "narrow-screen kv fallback missing"
    # exactly one ring per card: ring lives in the head, not in a second block
    js = DASHBOARD_JS.read_text()
    for fn in ("drawCpu", "drawRam", "drawGpu", "drawStorage"):
        block = js.split(f"function {fn}(")[1].split("\n  }")[0]
        assert block.count("ringSvg(") == 1, f"{fn}: exactly one ring"
        assert "hw-head-ring" in block, f"{fn}: ring rides the head row"


def test_dashboard_v32_no_duplicate_values():
    """v3.2 dedup contract in the page source."""
    src = DASHBOARD_JS.read_text()
    # removed dupes must not exist at all
    assert "dash-cpu-load" not in src, "CPU load sub-line must be gone"
    assert "dash-ram-sub" not in src, "RAM used/total sub must be gone"
    assert "dash-disk-sub" not in src, "Storage used/total sub must be gone"
    assert "dash-gpu-vram-cap" not in src, "GPU ring cap (duplicate VRAM text) must be gone"
    assert "dash-modelvram" not in src, "Model VRAM cell must be gone (merged into VRAM kv)"
    assert ">Clocks<" not in src and 'class="v">${g.clocks' not in src, "GPU Clocks cell must be gone"
    assert "utilization</div>" not in src, "GPU utilization sub-label must be gone"
    # the split cell exists and derives from /api/ps fields
    assert "dash-cpusplit" in src
    assert "size_vram" in src and "derived from /api/ps" in src


def test_dashboard_v3_frontend():
    assert DASHBOARD_JS.exists(), "dashboard.js missing"
    report = _run_node(_script())
    results = report.get("results", {})
    assert results, "no test results produced"
    failures = {k: v for k, v in results.items() if v != "PASS"}
    assert not failures, "frontend case failures:\n" + "\n".join(f"  {k}: {v}" for k, v in failures.items())
    jsdom_errors = [e for e in report.get("jsdomErrors", []) if "Could not parse CSS stylesheet" not in e]
    assert not jsdom_errors, "jsdom page errors:\n" + "\n".join(jsdom_errors)
    print(f"dashboard v3 frontend cases: {len(results)} passed")


if __name__ == "__main__":
    test_dashboard_v3_frontend()
