"""PROCESSOR frontend data handling — headless render test via node.

Extracts the PROCESSOR renderer from the real webui/static/js/pages/dashboard.js
and drives it against representative API payloads (good data, multiple GPUs,
GPU-less host, missing temperature, unreachable host, not configured).
No browser, no network, no real hardware. Skipped when node is absent.
"""
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

DASHBOARD = Path(__file__).resolve().parent.parent / "webui" / "static" / "js" / "pages" / "dashboard.js"

CASES = """
const cases = {
  good: { available: true, host: 'ollama-server',
          cpu: { utilization: 42, cores_physical: 8, cores_logical: 16, load: [3.2, 2.8, 2.4] },
          gpu_available: true,
          gpus: [{ index: 0, name: 'Tesla V100 16GB', utilization: 74,
                   memory_used: 12.4e9, memory_total: 16.16e9, memory_utilization: 46,
                   temperature: 64 }],
          ollama: { online: true, api_online: true, cli_fallback_used: false,
                    running_models: [ { name: 'deepseek-coder-v2:latest', size_vram: 6.1e9 },
                                      { name: 'qwen3:8b', size_vram: 1.2e9 } ],
                    vram_used: 7.3e9 } },
  multiGpu: { available: true, host: 'x', cpu: { utilization: 10, load: [1, 1, 1] }, gpu_available: true,
              gpus: [ {index: 0, name: 'A', utilization: 1, memory_used: 1e9, memory_total: 16e9, temperature: 50},
                      {index: 1, name: 'B', utilization: 99, memory_used: 15.9e9, memory_total: 16e9, temperature: 80} ],
              ollama: { online: true, running_models: [], vram_used: 0 } },
  noGpu: { available: true, host: 'x', cpu: { utilization: 5, load: [1] }, gpu_available: false,
           gpu_reason: 'no NVIDIA GPU', gpus: [], ollama: { online: true, running_models: [] } },
  noTemp: { available: true, host: 'x', cpu: { utilization: 5, load: [1] }, gpu_available: true,
            gpus: [{ index: 0, name: 'C', utilization: 9, memory_used: 1e9, memory_total: 2e9, temperature: null }],
            ollama: { online: true, running_models: [] } },
  ollamaDown: { available: true, host: 'x', cpu: { utilization: 5, load: [1] }, gpu_available: true,
                gpus: [{index:0,name:'G',utilization:1,memory_used:1e9,memory_total:2e9,temperature:40}],
                ollama: { online: false, api_online: false, cli_fallback_used: false, running_models: [], vram_used: 0 } },
  down: { available: false, reason: 'CPU data unavailable', cpu: null, gpus: [], gpu_available: false,
          ollama: { online: false, running_models: [] } },
  garbage: { available: true, host: 'x', cpu: { utilization: null, load: null }, gpu_available: true,
             gpus: [{ index: 0 }], ollama: { online: true, running_models: [{ name: 'm' }] } },
};
let failures = 0;
for (const [name, data] of Object.entries(cases)) {
  const el = document.getElementById('proc-body');
  renderProcessor(data);
  const html = el.innerHTML;
  const checks = {
    good: () => html.includes('42%') && html.includes('Tesla V100 16GB') &&
                html.includes('Load 3.2 / 2.8 / 2.4') && html.includes('16 cores') && html.includes('64\\u00b0C') &&
                html.includes('deepseek-coder-v2:latest') && html.includes('OLLAMA') &&
                html.includes('Total VRAM in use'),
    multiGpu: () => (html.match(/proc-card/g) || []).length === 4 &&
                   html.includes('#0') && html.includes('#1') && html.includes('99%') && html.includes('80\\u00b0C'),
    noGpu: () => html.includes('GPU information unavailable') && html.includes('no NVIDIA GPU') && html.includes('5%'),
    noTemp: () => html.includes('NOT AVAILABLE'),
    ollamaDown: () => html.includes('Ollama information unavailable') && html.includes('64') === false,
    down: () => html.includes('Processor information unavailable') && html.includes('CPU data unavailable'),
    garbage: () => html.includes('\\u2014') && !html.includes('NaN') && !html.includes('undefined') && !html.includes('null'),
  }[name];
  if (checks()) { console.log(name, 'PASS'); }
  else { failures++; console.log(name, 'FAIL', html.slice(0, 400)); }
}
process.exit(failures ? 1 : 0);
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_processor_frontend_render_cases():
    src = DASHBOARD.read_text()
    start = src.index("function fmtLoad")
    end = src.index("async function refreshProcessor")
    snippet = src[start:end]

    harness = (
        "const cache = {};\n"
        "global.document = { getElementById: (id) => (cache[id] ||= { classList: { add: () => {}, remove: () => {} },"
        " _h: '', set innerHTML(v) { this._h = v; }, get innerHTML() { return this._h; } }) };\n"
        "global.fmtBytes = (b) => (b == null ? '\\u2014' : (b / 1024 ** 3).toFixed(1) + ' GB');\n"
        "global.esc = (s) => String(s);\n"
        + snippet + "\n" + CASES + "\n"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(harness)
        path = f.name
    try:
        r = subprocess.run(["node", path], capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, f"frontend render cases failed:\n{r.stdout}\n{r.stderr}"
    finally:
        Path(path).unlink(missing_ok=True)


def test_processor_polling_interval_within_spec():
    """Poll interval must be in the 2-5s window required by the task."""
    src = DASHBOARD.read_text()
    assert "setInterval(refreshProcessor, 3000)" in src
    assert "/api/system/processor" in src
