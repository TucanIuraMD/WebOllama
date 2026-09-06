"""Agents page frontend — headless render/interaction test via node.

Loads the real webui/static/js/pages/agents.js into a minimal DOM stub and
drives the actual user flows: matrix render, filter chips (regression: they
must stay clickable after re-render), cell editor save/reset, and the admin
"+ Add Agent" / "+ Add Capability" forms — including API error display.
No browser, no network, no Ollama. Skipped when node is absent.
"""
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

AGENTS_JS = Path(__file__).resolve().parent.parent / "webui" / "static" / "js" / "pages" / "agents.js"

HARNESS = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');

// ---- fake DOM ----
const els = {};
function makeEl(id) {
  const o = { id, _html: '', style: {}, value: '', checked: false, textContent: '',
              dataset: {}, disabled: false, appendChild() {}, remove() {}, focus() {},
              classList: { add() {}, remove() {}, toggle() {} } };
  Object.defineProperty(o, 'innerHTML', { get() { return o._html; }, set(v) { o._html = String(v); } });
  return o;
}
global.document = {
  getElementById: (id) => (els[id] ||= makeEl(id)),
  querySelector: () => null,
  querySelectorAll: () => [],
  createElement: () => ({ style: {}, appendChild() {}, remove() {} }),
  addEventListener() {},
  body: makeEl('body'),
};
global.window = global;
global.esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
global.toast = () => {};
let modalOpen = false; let modalHtml = '';
global.openModal = (html) => { modalOpen = true; modalHtml = html;
  global.document.getElementById('modal-body').innerHTML = html; };
global.closeModal = () => { modalOpen = false; };

// ---- mock app + API ----
const baseData = {
  ollama_online: true,
  agents: [
    { id: 1, name: 'Hermes', slug: 'hermes', description: '', enabled: 1 },
    { id: 2, name: 'OpenCode', slug: 'opencode', description: '', enabled: 1 },
  ],
  capabilities: [
    { id: 1, name: 'Coding', slug: 'coding' },
    { id: 2, name: 'Chat', slug: 'chat' },
  ],
  models: [
    { name: 'qwen3:8b', size: 1000, parameter_size: '8B', quantization_level: 'Q4' },
    { name: 'llama3.2:1b', size: 500 },
  ],
  assessments: [
    { id: 7, model: 'qwen3:8b', agent_id: 2, status: 'works', note: 'ok',
      tested_at: 1757000000, agent_name: 'OpenCode', agent_slug: 'opencode',
      capabilities: [{ id: 1, name: 'Coding', slug: 'coding' }] },
  ],
  counts: { models: 2, agents: 2, assessments: 1, tested: 1 },
};
let apiCalls = [];
const responders = {};
global.App = { state: { user: { username: 'admin', role: 'admin' } } };
global.API = {
  async get(url) {
    apiCalls.push(['GET', url]);
    const r = responders['GET ' + url];
    if (r) return r();
    if (url === '/api/agents/matrix') return JSON.parse(JSON.stringify(baseData));
    throw new Error('unexpected GET ' + url);
  },
  async post(url, body) {
    apiCalls.push(['POST', url, body]);
    const r = responders['POST ' + url];
    if (!r) throw new Error('unexpected POST ' + url);
    return r(body);
  },
  async del(url) {
    apiCalls.push(['DELETE', url]);
    const r = responders['DELETE ' + url];
    if (!r) throw new Error('unexpected DELETE ' + url);
    return r();
  },
};
function resetApi() { apiCalls = []; for (const k of Object.keys(responders)) delete responders[k]; }

let failures = 0;
async function check(name, fn) {
  try { await fn(); console.log(name, 'PASS'); }
  catch (e) { failures++; console.log(name, 'FAIL:', e.message); }
}
function assert(cond, msg) { if (!cond) throw new Error(msg || 'assertion failed'); }
const inp = (id) => global.document.getElementById(id); // auto-creates stub
const dashes = (html) => (html.match(/—/g) || []).length;

async function main() {
  eval(src); // registers Pages.agents + Actions handlers
  const page = global.document.createElement('page');

  // ---- admin render ----
  resetApi();
  await global.Pages.agents.render(page);
  let html = page.innerHTML;
  await check('admin sees + Add Agent / + Add Capability', () => {
    assert(html.includes('+ Add Agent') && html.includes('+ Add Capability'));
    assert(els['agents-admin-row'].style.display === 'flex');
  });
  await check('matrix renders agent columns and model rows', () => {
    const w = els['agents-matrix-wrap'].innerHTML;
    assert(w.includes('>Hermes<') && w.includes('>OpenCode<'));
    assert(w.includes('qwen3:8b') && w.includes('llama3.2:1b'));
  });
  await check('untested cells show dash, assessed cell shows check + capability', () => {
    const w = els['agents-matrix-wrap'].innerHTML;
    assert(dashes(w) === 3);
    assert(w.includes('✓') && w.includes('coding'));
  });
  await check('filter chips carry data-action (delegation survives re-render)', () => {
    assert(els['agents-agent-row'].innerHTML.includes('data-action="agents-filter"'));
    assert(els['agents-cap-row'].innerHTML.includes('data-action="agents-filter"'));
    assert(els['agents-status-row'].innerHTML.includes('data-action="agents-filter"'));
  });
  await check('status filter applies and toggles off', () => {
    global.Actions['agents-filter']({ group: 'status', value: 'works' });
    let w = els['agents-matrix-wrap'].innerHTML;
    assert(w.includes('qwen3:8b') && !w.includes('llama3.2:1b'));
    global.Actions['agents-filter']({ group: 'status', value: 'works' });
    w = els['agents-matrix-wrap'].innerHTML;
    assert(w.includes('llama3.2:1b'));
  });

  // ---- cell editor + save ----
  resetApi();
  global.Actions['agents-cell']({ model: 'llama3.2:1b', agent: '1' });
  await check('cell editor opens with status/capabilities/note', () => {
    assert(modalOpen);
    assert(modalHtml.includes('ed-status') && modalHtml.includes('Good'));
    assert(modalHtml.includes('ed-caps') && modalHtml.includes('Coding'));
    assert(modalHtml.includes('ed-note'));
    assert(!modalHtml.includes('Reset to untested')); // nothing saved yet
  });
  global.document.querySelector = (sel) => (sel.includes('ed-status') ? { value: 'good' } : null);
  global.document.querySelectorAll = (sel) => (sel.includes('ed-caps') ? [{ value: '1' }] : []);
  inp('ed-note').value = 'manual note';
  responders['POST /api/agents/assessments'] = () => ({ ok: true, assessment: {
    id: 9, model: 'llama3.2:1b', agent_id: 1, status: 'good', note: 'manual note',
    tested_at: 1757000000, agent_name: 'Hermes', agent_slug: 'hermes',
    capabilities: [{ id: 1, name: 'Coding', slug: 'coding' }] } });
  await global.AgentsPage.saveCell();
  await check('save posts assessment with correct payload', () => {
    const call = apiCalls.find((c) => c[0] === 'POST' && c[1] === '/api/agents/assessments');
    assert(call && call[2].model === 'llama3.2:1b' && call[2].agent_id === 1 &&
           call[2].status === 'good' && call[2].note === 'manual note' &&
           JSON.stringify(call[2].capabilities) === '[1]');
  });
  await check('modal closed after save', () => assert(!modalOpen));
  await check('new status + note + capability visible in matrix immediately', () => {
    const w = els['agents-matrix-wrap'].innerHTML;
    assert(w.includes('status: Good') && w.includes('note: manual note'));
    assert(dashes(w) === 2); // one cell less untested
  });

  // ---- reset flow ----
  responders['DELETE /api/agents/assessments/9'] = () => ({ ok: true });
  global.Actions['agents-cell']({ model: 'llama3.2:1b', agent: '1' });
  await check('reset button shown for saved assessment', () => {
    assert(modalOpen && modalHtml.includes('Reset to untested'));
  });
  await global.AgentsPage.resetCell();
  await check('reset calls DELETE and cell back to dash', () => {
    const call = apiCalls.find((c) => c[0] === 'DELETE');
    assert(call && call[1] === '/api/agents/assessments/9');
    assert(dashes(els['agents-matrix-wrap'].innerHTML) === 3);
    assert(!els['agents-matrix-wrap'].innerHTML.includes('status: Good'));
    assert(!modalOpen);
  });

  // ---- add agent ----
  resetApi();
  global.document.querySelector = () => null;
  global.document.querySelectorAll = () => [];
  global.Actions['agents-add']();
  await check('agent form opens', () => {
    assert(modalOpen && modalHtml.includes('af-name') && modalHtml.includes('af-slug') &&
           modalHtml.includes('af-desc') && modalHtml.includes('af-enabled'));
  });
  inp('af-name').value = 'Docs Agent';
  inp('af-slug').value = '';
  inp('af-desc').value = 'docs env';
  inp('af-enabled').checked = true;
  responders['POST /api/agents/agents'] = () => ({ ok: true, agent: {
    id: 3, name: 'Docs Agent', slug: 'docs-agent', description: 'docs env', enabled: 1 } });
  await global.AgentsPage.submitAgent();
  await check('agent created; column + filter appear without reload', () => {
    const call = apiCalls.find((c) => c[1] === '/api/agents/agents');
    assert(call && call[2].name === 'Docs Agent' && call[2].enabled === true);
    assert(!modalOpen);
    assert(els['agents-matrix-wrap'].innerHTML.includes('>Docs Agent<'));
    assert(els['agents-agent-row'].innerHTML.includes('Docs Agent'));
  });

  // ---- add capability ----
  global.Actions['agents-add-cap']();
  inp('cf-name').value = 'OCR';
  inp('cf-slug').value = '';
  inp('cf-desc').value = 'image text';
  responders['POST /api/agents/capabilities'] = () => ({ ok: true, capability: {
    id: 3, name: 'OCR', slug: 'ocr', description: 'image text' } });
  await global.AgentsPage.submitCapability();
  await check('capability created; appears in filter and in cell editor', () => {
    assert(!modalOpen);
    assert(els['agents-cap-row'].innerHTML.includes('OCR'));
    global.Actions['agents-cell']({ model: 'llama3.2:1b', agent: '1' });
    assert(modalHtml.includes('OCR'));
  });

  // ---- duplicate slug error handling ----
  global.Actions['agents-add']();
  inp('af-name').value = 'Whatever';
  inp('af-slug').value = 'docs-agent'; // duplicate slug
  responders['POST /api/agents/agents'] = () => { throw new Error('agent slug already exists: docs-agent'); };
  await global.AgentsPage.submitAgent();
  await check('duplicate slug error shown in form, modal stays open', () => {
    assert(els['af-error'].textContent.includes('already exists'));
    assert(els['af-error'].style.display === 'block');
    assert(modalOpen);
  });

  // ---- non-admin gating ----
  global.closeModal(); // close the leftover duplicate-slug form
  global.App.state.user = { username: 'bob', role: 'user' };
  await global.Pages.agents.render(page);
  await check('non-admin: config buttons hidden', () => {
    assert(els['agents-admin-row'].style.display === 'none');
  });
  global.Actions['agents-add']();
  await check('non-admin: add actions are no-ops', () => assert(!modalOpen));

  process.exit(failures ? 1 : 0);
}

main().catch((e) => { console.error('HARNESS ERROR', e); process.exit(2); });
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_agents_frontend_flows():
    harness = HARNESS.replace("process.argv[2]", json_str(str(AGENTS_JS)))
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(harness)
        path = f.name
    try:
        r = subprocess.run(["node", path], capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, f"agents frontend flows failed:\n{r.stdout}\n{r.stderr}"
        assert "FAIL" not in r.stdout, r.stdout
    finally:
        Path(path).unlink(missing_ok=True)


def json_str(s: str) -> str:
    import json

    return json.dumps(s)


def test_agents_page_uses_delegated_filter_actions():
    """Regression: filter chips are re-rendered by drawFilters(), so they must
    use the global data-action delegation instead of one-time onclick binding."""
    src = AGENTS_JS.read_text()
    assert 'data-action="agents-filter"' in src
    assert 'window.Actions["agents-filter"]' in src
    assert "agents-add" in src and "agents-add-cap" in src
    assert "/api/agents/agents" in src
    assert "/api/agents/capabilities" in src
    assert "/api/agents/assessments" in src
