"""Electricity frontend — headless flow tests via node.

Loads the REAL webui/static/js/pages/electricity.js into the minimal DOM
stub and drives the states required by the Electricity v1 task:
loading, power unavailable (honest "data unavailable" + GPU reference,
never a fake total), power available (measured), tariff unset (explicit
"needs configuration" notice, no invented default), tariff set (cost shown
as calculated), history empty vs present, period cards with kWh, and the
polling contract (one page-scoped interval, cleared on re-render).
Skipped when node is absent.
"""
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

ELECTRICITY_JS = Path(__file__).resolve().parent.parent / "webui" / "static" / "js" / "pages" / "electricity.js"

HARNESS = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');

// ---- fake DOM ----
const els = {};
function makeNode() {
  const o = { _text: '', _html: '', style: {}, value: '', checked: false, textContent: '',
              dataset: {}, disabled: false, className: '', id: '', href: '',
              appendChild(c) { o._children.push(c); }, remove() {}, focus() {},
              addEventListener() {}, _children: [], scrollTop: 0, scrollHeight: 100,
              classList: { _s: new Set(), add(c) { o.classList._s.add(c); }, remove(c) { o.classList._s.delete(c); }, toggle(c, on) { on ? o.classList._s.add(c) : o.classList._s.delete(c); }, contains(c) { return o.classList._s.has(c); } } };
  Object.defineProperty(o, 'innerHTML', { get() { return o._html; }, set(v) { o._html = String(v); o._children = []; } });
  Object.defineProperty(o, 'outerHTML', { get() { return o._html; }, set(v) { o._html = String(v); } });
  o.querySelector = () => makeNode(); // save-button stub: addEventListener is a no-op
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
global.toast = () => {};

// minimal Charts/seriesChart stubs
global.Charts = { destroyAll() {}, make() { return null; } };
global.seriesChart = (id) => ({ id, load() {}, push() {} });

// ---- API mock ----
let responders = {};
let calls = [];
global.API = {
  get: async (url) => { calls.push(['GET', url]); const r = responders['GET ' + url.split('?')[0]]; if (!r) throw new Error('no mock for GET ' + url); return typeof r === 'function' ? r() : r; },
  put: async (url, body) => { calls.push(['PUT', url]); const r = responders['PUT ' + url]; if (!r) throw new Error('no mock for PUT ' + url); return typeof r === 'function' ? r(body) : r; },
  post: async () => ({}), del: async () => ({}),
};
function resetApi() { responders = {}; calls = []; }
global.__calls = calls;

// fake timers
let timers = [];
global.setInterval = (fn, ms) => { const t = { fn, ms, cleared: false }; timers.push(t); return t; };
global.clearInterval = (t) => { if (t) t.cleared = true; };

let failures = 0;
async function check(name, fn) {
  try { await fn(); console.log(name, 'PASS'); }
  catch (e) { failures++; console.log(name, 'FAIL:', e.message); }
}
function assert(cond, msg) { if (!cond) throw new Error(msg || 'assertion failed'); }

// ---- API payloads ----
const UNAVAILABLE = {
  available: false, measured: false, source: null, watts: null,
  gpu_power: { watts: 185.0, measured: true,
               note: 'GPU power draw only — NOT total server power',
               per_device: [{ index: 0, name: 'Tesla V100', watts: 185.0, measured: true }] },
  components: { cpu: { watts: null, measured: false, source: null },
                platform: { watts: null, measured: false, source: null } },
  reason: 'no host-level power telemetry on this machine (RAPL unreadable/denied, no hwmon power sensors) — total server power is UNAVAILABLE; GPU power draw (if any) is shown separately and is NOT a total',
  ts: 1000,
};
const AVAILABLE = {
  available: true, measured: true, source: 'hwmon', watts: 233.5,
  gpu_power: { watts: 185.0, measured: true, note: 'GPU power draw only — NOT total server power',
               per_device: [{ index: 0, name: 'Tesla V100', watts: 185.0, measured: true }] },
  components: { cpu: { watts: null, measured: false, source: null },
                platform: { watts: 233.5, measured: true, source: 'hwmon' } },
  reason: null, ts: 1000,
};
const CFG_UNSET = { tariff: null, currency: 'lei', tariff_is_configured: false,
                    notice: 'tariff not configured — set lei/kWh to see cost; no default is invented' };
const CFG_SET = { tariff: 5.0, currency: 'lei', tariff_is_configured: true, notice: null };

async function main() {
  global.App = { state: { currentPage: 'electricity', snapshot: null } };
  eval(src);
  const page = global.Pages.electricity;
  const container = makeEl('page-container');

  async function renderWith(mocks) {
    resetApi();
    for (const k of Object.keys(els)) delete els[k];
    els['page-container'] = container;
    Object.assign(responders, mocks);
    timers = [];
    await page.render(container);
    // drawChart/renderPeriods are fire-and-forget async chains — let them settle
    await new Promise((r) => setImmediate(r));
    await new Promise((r) => setImmediate(r));
  }
  function liveHtml() { return container._html; }
  function cfgHtml() { return els['elec-config'] ? els['elec-config']._html : ''; }
  function periodsHtml() { return els['elec-periods'] ? els['elec-periods']._html : ''; }

  await check('unavailable power: honest message, no fake total, GPU shown as reference only', async () => {
    await renderWith({
      'GET /api/electricity': UNAVAILABLE,
      'GET /api/electricity/config': CFG_UNSET,
      'GET /api/electricity/history': { points: [] },
      'GET /api/electricity/summary': { kwh: null, points: 0, tariff_configured: false },
    });
    const h = liveHtml();
    assert(h.includes('data unavailable'), 'no "data unavailable" message');
    assert(h.includes('NOT total server power') || h.includes('not</b> total server power'), 'GPU reference mislabeled');
    assert(!/Total server power<\/div>\s*<div class="metric-value mono"[^>]*>\s*\d/.test(h), 'a numeric total was rendered despite no source');
    assert(h.includes('185.0 W'), 'GPU reference value missing');
    assert(h.includes('Tariff settings'), 'config section missing');
    assert(cfgHtml().includes('tariff not configured'), 'no needs-configuration notice');
  });

  await check('available power: measured total + component tiles render', async () => {
    await renderWith({
      'GET /api/electricity': AVAILABLE,
      'GET /api/electricity/config': CFG_SET,
      'GET /api/electricity/history': { points: [] },
      'GET /api/electricity/summary': { kwh: null, points: 0, tariff_configured: true },
    });
    const h = liveHtml();
    assert(h.includes('233.5 W'), 'measured total missing');
    assert(h.includes('measured'), 'measured badge missing');
    assert(h.includes('GPU draw (reference)'), 'GPU reference tile missing');
    assert(h.includes('not</b> total server power'), 'GPU honesty note missing');
  });

  await check('tariff unset: no cost numbers anywhere, notice shown', async () => {
    await renderWith({
      'GET /api/electricity': AVAILABLE,
      'GET /api/electricity/config': CFG_UNSET,
      'GET /api/electricity/history': { points: [] },
      'GET /api/electricity/summary': { kwh: 0.42, points: 10, tariff_configured: false },
    });
    const p = periodsHtml();
    assert(p.includes('0.4200 kWh'), 'kwh missing');
    assert(p.includes('tariff not configured'), 'period cost notice missing');
    assert(cfgHtml().includes('tariff not configured'), 'config notice missing');
    assert(!p.includes('Cost'), 'cost rendered without configured tariff');
  });

  await check('tariff set: cost shown as calculated', async () => {
    await renderWith({
      'GET /api/electricity': AVAILABLE,
      'GET /api/electricity/config': CFG_SET,
      'GET /api/electricity/history': { points: [] },
      'GET /api/electricity/summary': { kwh: 0.42, points: 10, tariff_configured: true, cost: 2.1, currency: 'lei', measured: true },
    });
    const p = periodsHtml();
    assert(p.includes('2.10 lei'), 'calculated cost missing');
    assert(p.toLowerCase().includes('calculated'), 'cost not labeled as calculated');
  });

  await check('history present: chart loads points; empty: empty-state message', async () => {
    let loaded = null;
    global.seriesChart = () => ({ load(points) { loaded = points; }, push() {} });
    await renderWith({
      'GET /api/electricity': AVAILABLE,
      'GET /api/electricity/config': CFG_SET,
      'GET /api/electricity/history': { points: [{ ts: 1, data: { watts: 100 } }, { ts: 2, data: { watts: 120 } }] },
      'GET /api/electricity/summary': { kwh: null, points: 0, tariff_configured: false },
    });
    assert(loaded && loaded.length === 2, 'chart did not receive history points');

    loaded = null;
    await renderWith({
      'GET /api/electricity': AVAILABLE,
      'GET /api/electricity/config': CFG_SET,
      'GET /api/electricity/history': { points: [] },
      'GET /api/electricity/summary': { kwh: null, points: 0, tariff_configured: false },
    });
    assert(loaded === null, 'chart loaded with no data');
    // the stub DOM has no real nesting: the message lands on the chart node itself
    const chartNode = els['elec-chart'];
    const msg = (chartNode && chartNode._html) || liveHtml();
    assert(String(msg).includes('no electricity history yet'), 'empty-state message missing');
  });

  await check('api error: error state, no crash', async () => {
    resetApi();
    responders['GET /api/electricity'] = () => { throw new Error('boom'); };
    timers = [];
    await page.render(container);
    assert(liveHtml().includes('API error'), 'error state missing');
    assert(liveHtml().includes('boom'), 'error detail missing');
  });

  await check('polling contract: exactly one page-scoped 5s interval, cleared on re-render', async () => {
    await renderWith({
      'GET /api/electricity': AVAILABLE,
      'GET /api/electricity/config': CFG_SET,
      'GET /api/electricity/history': { points: [] },
      'GET /api/electricity/summary': { kwh: null, points: 0, tariff_configured: false },
    });
    assert(timers.length === 1, 'expected exactly one interval, got ' + timers.length);
    assert(timers[0].ms === 5000, 'poll interval should be 5000ms');
    // navigate away destroys the timer
    global.App.state.currentPage = 'dashboard';
    timers[0].fn();
    global.App.state.currentPage = 'electricity';
    await renderWith({
      'GET /api/electricity': AVAILABLE,
      'GET /api/electricity/config': CFG_SET,
      'GET /api/electricity/history': { points: [] },
      'GET /api/electricity/summary': { kwh: null, points: 0, tariff_configured: false },
    });
    assert(timers.length === 1, 're-render leaked an extra timer');
  });

  process.exit(failures ? 1 : 0);
}

main().catch((e) => { console.error('HARNESS ERROR', e); process.exit(2); });
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_electricity_frontend_flows():
    harness = HARNESS.replace("process.argv[2]", json.dumps(str(ELECTRICITY_JS)))
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(harness)
        path = f.name
    try:
        r = subprocess.run(["node", path], capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, f"electricity frontend flows failed:\n{r.stdout}\n{r.stderr}"
        assert "FAIL" not in r.stdout, r.stdout
    finally:
        Path(path).unlink(missing_ok=True)


def test_electricity_polling_discipline():
    """Polling contract: electricity poll is the ONLY interval on the page,
    page-scoped, and explicitly cleared before each re-render."""
    src = ELECTRICITY_JS.read_text()
    assert src.count("setInterval(") == 1, "more than one polling loop"
    assert "clearInterval(elecTimer)" in src
    assert 'App.state.currentPage !== "electricity"' in src  # page-scoped
    # the page must not read the WS snapshot GPU block as power data
    assert "snap.gpu" not in src


def test_dashboard_links_to_electricity_not_charts():
    """Dashboard gets a compact LINK to Electricity, never electric charts."""
    dash = (Path(__file__).resolve().parent.parent / "webui" / "static" / "js" / "pages" / "dashboard.js").read_text()
    assert 'href="#electricity"' in dash
    assert "/api/electricity" not in dash  # no polling of electricity from dashboard
    idx = (Path(__file__).resolve().parent.parent / "webui" / "static" / "index.html").read_text()
    assert 'data-page="electricity"' in idx
    assert 'electricity.js' in idx
