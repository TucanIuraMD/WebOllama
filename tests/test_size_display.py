"""Size display formatting — Models UI must match `ollama ls`.

Root cause: the shared fmtBytes formatter divided by 1024 (binary units,
i.e. GiB) but labelled the result "GB". Ollama CLI prints model sizes in
DECIMAL GB (bytes / 1_000_000_000), so a 6_700_000_000-byte model showed
as "6.2 GB" in WebOllama while `ollama ls` correctly showed "6.7 GB".

Fix: fmtBytes now uses decimal steps (bytes / 1000, threshold 1000). The
displayed number is directly comparable with the CLI. API values are
passed through untouched.

These tests load the REAL webui/static/js/api.js into node and pin the
formatting contract. Skipped when node is absent.
"""
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

API_JS = Path(__file__).resolve().parent.parent / "webui" / "static" / "js" / "api.js"

HARNESS = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
// load the real api.js: it defines API (needs fetch-less stubs) + fmtBytes/fmtSpeed/fmtNum/esc
global.document = { cookie: '' };
global.fetch = async () => ({ ok: true, text: async () => '', status: 200 });
global.location = { protocol: 'http:' };
global.window = global;
eval(src);

let failures = 0;
function eq(name, got, want) {
  const ok = got === want;
  if (!ok) failures++;
  console.log(ok ? 'PASS' : 'FAIL', name, '→', JSON.stringify(got), ok ? '' : ('want ' + JSON.stringify(want)));
}
function near(name, got, want) {
  const ok = got.startsWith(want);
  if (!ok) failures++;
  console.log(ok ? 'PASS' : 'FAIL', name, '→', JSON.stringify(got), ok ? '' : ('want prefix ' + JSON.stringify(want)));
}

// --- the regression from the task: 6.7 GB model (ollama ls) ---
eq('6.7 GB (6_700_000_000 B) ≈ ollama ls, NOT 6.2', fmtBytes(6_700_000_000), '6.7 GB');
eq('ornith-vision-like 6_657_199_309 B rounds to 6.7', fmtBytes(6_657_199_309), '6.7 GB');
eq('5.2 GB', fmtBytes(5_200_000_000), '5.2 GB');
eq('no binary divide anywhere: 1_024_000_000 → 1.0 GB', fmtBytes(1_024_000_000), '1.0 GB');
eq('1_073_741_824 B (1 GiB) → 1.1 GB decimal', fmtBytes(1_073_741_824), '1.1 GB');

// --- MB range ---
eq('1_500_000 B → 1.5 MB', fmtBytes(1_500_000), '1.5 MB');
eq('999_999 B → 1000.0 KB (step boundary below next unit)', fmtBytes(999_999), '1000.0 KB');
eq('500_000 B → 500.0 KB', fmtBytes(500_000), '500.0 KB');
eq('12_345_678 B → 12.3 MB', fmtBytes(12_345_678), '12.3 MB');

// --- large sizes ---
eq('2_000_000_000_000 B → 2.0 TB', fmtBytes(2_000_000_000_000), '2.0 TB');
eq('15_400_000_000 B (V100 used VRAM) → 15.4 GB', fmtBytes(15_400_000_000), '15.4 GB');
eq('1_234_567_890_123 B → 1.2 TB', fmtBytes(1_234_567_890_123), '1.2 TB');

// --- rounding + precision contract (existing dp behaviour preserved) ---
eq('default dp=1 at GB: 6_749_999_999 → 6.7', fmtBytes(6_749_999_999), '6.7 GB');
eq("default dp=1 at GB: 6_750_000_000 → 6.8 (toFixed rounds half up)", fmtBytes(6_750_000_000), "6.8 GB");
eq('default dp=1 at GB: 6_800_000_000 → 6.8', fmtBytes(6_800_000_000), '6.8 GB');
eq('explicit dp=0: 6_700_000_000 → 7 GB', fmtBytes(6_700_000_000, 0), '7 GB');
eq('explicit dp=2: 6_657_199_309 → 6.66 GB', fmtBytes(6_657_199_309, 2), '6.66 GB');
eq('bytes: 999 → 999 B (integer, no decimals at i=0)', fmtBytes(999), '999 B');
eq('threshold: exactly 1000 → 1.0 KB', fmtBytes(1000), '1.0 KB');

// --- edge cases unchanged ---
eq('null → —', fmtBytes(null), '—');
eq('undefined → —', fmtBytes(undefined), '—');
eq('NaN → —', fmtBytes(NaN), '—');
eq('0 → 0 B', fmtBytes(0), '0 B');
eq('string number passes through Number()', fmtBytes('6700000000'), '6.7 GB');

// --- fmtSpeed shares the same decimal contract ---
eq('fmtSpeed 6_700_000_000 B/s → 6.7 GB/s', fmtSpeed(6_700_000_000), '6.7 GB/s');
eq('fmtSpeed 0 → 0 B/s', fmtSpeed(0), '0 B/s');

process.exit(failures ? 1 : 0);
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_fmt_bytes_decimal_units_match_ollama_cli():
    harness = HARNESS.replace("process.argv[2]", repr(str(API_JS)))
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(harness)
        path = f.name
    try:
        r = subprocess.run(["node", path], capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, f"size display tests failed:\n{r.stdout}\n{r.stderr}"
        assert "FAIL" not in r.stdout, r.stdout
    finally:
        Path(path).unlink(missing_ok=True)


def test_fmt_bytes_is_decimal_in_source():
    """Source pin: the formatter must not use binary steps anymore, and it
    is the ONLY byte formatter in the frontend (single source of truth)."""
    src = API_JS.read_text()
    assert "n /= 1000" in src and "n >= 1000" in src
    assert "n /= 1024" not in src and "n >= 1024" not in src
    # api.js loads BEFORE every page in index.html → all pages get the fix
    html = (API_JS.parents[1] / "index.html").read_text()
    assert html.index("/js/api.js") < html.index("/js/pages/models.js")
    assert html.index("/js/api.js") < html.index("/js/pages/running.js")
    assert html.index("/js/api.js") < html.index("/js/pages/dashboard.js")


def test_no_binary_divide_in_model_pages():
    """Pages must not keep their own binary size math — display sizes come
    from the shared decimal formatter (API values untouched)."""
    import re
    base = API_JS.parent / "pages"  # js/pages — correct
    for page in ("models.js", "running.js"):
        src = (base / page).read_text()
        assert not re.search(r"/\s*1024\s*\*\*\s*3", src), f"{page} still divides by 1024**3"
