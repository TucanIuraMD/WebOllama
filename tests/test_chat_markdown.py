"""Chat v3 markdown rendering — real md.js + vendored marked/DOMPurify via node.

Runs the actual webui/static/js/md.js bridge with the actual vendored
marked.min.js and purify.min.js inside jsdom and asserts the exact
rendering/SECURITY behavior the chat page relies on:

  - headers/bold/italic/lists/quotes/inline code/fenced code render;
  - fenced code keeps the language- class (future highlight.js hook);
  - <script>, event handlers and javascript: URLs are stripped (XSS);
  - the bridge degrades to ESCAPED text when a lib is missing/broken;
  - chat.js wires replies through renderMarkdown.

Skipped when node or jsdom is unavailable (jsdom is optional).
"""
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

WEBUI = Path(__file__).resolve().parent.parent / "webui" / "static" / "js"
MD_JS = WEBUI / "md.js"
MARKED_JS = WEBUI / "vendor" / "marked.min.js"
PURIFY_JS = WEBUI / "vendor" / "purify.min.js"
CHAT_JS = WEBUI / "pages" / "chat.js"

HARNESS = r"""
const fs = require('fs');
const { JSDOM } = require('jsdom');

const dom = new JSDOM('<!doctype html><html><body></body></html>', { url: 'http://localhost/' });

// marked: UMD prefers module.exports under node — load into an isolated
// module scope, then expose on the jsdom window like the browser bundle.
const markedSrc = fs.readFileSync(process.argv[2], 'utf8');
const mfn = new Function('module', 'exports', 'define', markedSrc);
const mmod = { exports: {} };
mfn(mmod, mmod.exports, undefined);

// DOMPurify: needs a real DOM — hand it the jsdom window.
const dpSrc = fs.readFileSync(process.argv[3], 'utf8');
const dfn = new Function('module', 'exports', 'define', 'window', 'document', dpSrc);
const dmod = { exports: {} };
dfn(dmod, dmod.exports, undefined, dom.window, dom.window.document);

const w = dom.window;
w.marked = mmod.exports;
w.DOMPurify = dmod.exports;

// Load the real bridge against the jsdom window.
const mdSrc = fs.readFileSync(process.argv[4], 'utf8');
const mdFn = new Function('window', mdSrc);
mdFn(w);

let failures = 0;
async function check(name, fn) {
  try { await fn(); console.log(name, 'PASS'); }
  catch (e) { failures++; console.log(name, 'FAIL:', e.message); }
}
function assert(cond, msg) { if (!cond) throw new Error(msg || 'assertion failed'); }

async function main() {
  await check('libs loaded: marked + DOMPurify supported', () => {
    assert(w.__markdownAvailable() === true, 'bridge reports libs unavailable');
  });

  const SAMPLE = [
    '# Заголовок',
    '',
    '**жирный текст**',
    '',
    '*курсив*',
    '',
    '- список',
    '',
    '> цитата',
    '',
    '`inline code`',
    '',
    '```python',
    'def hello():',
    '    print("Hello")',
    '```',
  ].join('\n');

  let html = '';
  await check('the manual-check sample renders fully (no raw # * ` left)', () => {
    html = w.renderMarkdown(SAMPLE);
    assert(html.includes('<h1>Заголовок</h1>'), 'h1 missing: ' + html.slice(0, 80));
    assert(html.includes('<strong>жирный текст</strong>'), 'bold missing');
    assert(html.includes('<em>курсив</em>'), 'italic missing');
    assert(html.includes('<li>список</li>'), 'list missing');
    assert(html.includes('<blockquote>'), 'quote missing');
    assert(html.includes('<code>inline code</code>'), 'inline code missing');
    assert(html.includes('language-python'), 'code language class missing');
    assert(html.includes('print(&quot;Hello&quot;)') || html.includes('print("Hello")'), 'code content missing');
    // no raw markdown syntax leaking into the output
    assert(!/<p>\s*#/.test(html), 'raw # leaked');
    assert(!/\*\*жирный/.test(html), 'raw ** leaked');
  });

  await check('tables and strikethrough (GFM) render', () => {
    const h = w.renderMarkdown('| a | b |\n|---|---|\n| 1 | 2 |\n\n~~gone~~');
    assert(h.includes('<table>'), 'table missing: ' + h);
    assert(h.includes('<th>a</th>') && h.includes('<td>1</td>'), 'table cells missing');
    assert(h.includes('<del>gone</del>'), 'strikethrough missing');
  });

  await check('XSS: <script> stripped', () => {
    const h = w.renderMarkdown('hello\n\n<script>alert(1)<\/script>');
    assert(!h.includes('<script'), 'script tag survived: ' + h);
    assert(!h.includes('alert(1)'), 'script content survived');
    assert(h.includes('hello'), 'legit text lost');
  });

  await check('XSS: onerror handler stripped, safe img kept', () => {
    const h = w.renderMarkdown('<img src="x.png" onerror="alert(1)">');
    assert(!h.includes('onerror'), 'handler survived: ' + h);
    assert(h.includes('<img src="x.png">'), 'safe img lost: ' + h);
  });

  await check('XSS: javascript: href stripped, https href kept', () => {
    const h = w.renderMarkdown('[bad](javascript:alert(1))\n\n[good](https://example.com)');
    assert(!/href="javascript:/i.test(h), 'js href survived: ' + h);
    assert(h.includes('href="https://example.com"'), 'safe href lost');
    assert(h.includes('>bad<') && h.includes('>good<'), 'link text lost');
  });

  await check('XSS: iframe/object/embed/form stripped', () => {
    const h = w.renderMarkdown('<iframe src="https://evil.example"><\/iframe><object data="x"><\/object><form action="/steal"><input name="pw"><\/form>');
    assert(!h.includes('<iframe') && !h.includes('<object') && !h.includes('<form') && !h.includes('<input'),
           'forbidden tag survived: ' + h);
  });

  await check('degrade: broken marked → escaped text, never unsanitized', () => {
    const saved = w.marked;
    w.marked = { parse: () => { throw new Error('boom'); } };
    try {
      const h = w.renderMarkdown('# Hi <b>there</b>');
      assert(h.includes('&lt;b&gt;'), 'not escaped on parse failure: ' + h);
      assert(!h.includes('<b>'), 'raw HTML on parse failure');
    } finally { w.marked = saved; }
  });

  await check('degrade: DOMPurify missing → escaped text', () => {
    const saved = w.DOMPurify;
    w.DOMPurify = undefined;
    try {
      const h = w.renderMarkdown('**bold** <script>x<\/script>');
      assert(!h.includes('<strong>'), 'markdown rendered without sanitizer');
      assert(h.includes('&lt;script&gt;'), 'not escaped without sanitizer');
    } finally { w.DOMPurify = saved; }
  });

  await check('empty/undefined input → empty string', () => {
    assert(w.renderMarkdown('') === '');
    assert(w.renderMarkdown(undefined) === '');
    assert(w.renderMarkdown(null) === '');
  });

  await check('chat.js routes replies through renderMarkdown', () => {
    const chat = fs.readFileSync(process.argv[5], 'utf8');
    assert(chat.includes('bodyNode.innerHTML = renderMarkdown(reply)'), 'delta render missing');
    assert(chat.includes('if (reply) bodyNode.innerHTML = renderMarkdown(reply)'), 'final render missing');
    assert(chat.includes('chat-md'), 'body node class missing');
  });

  process.exit(failures ? 1 : 0);
}

main().catch((e) => { console.error('HARNESS ERROR', e); process.exit(2); });
"""


def _node_with_jsdom() -> bool:
    """True if node exists and jsdom resolves from common locations."""
    if shutil.which("node") is None:
        return False
    probe = "try{require('jsdom');console.log('ok')}catch(e){console.log('no')}"
    # resolution candidates: plain node, project dir, sibling .node_test
    # (convenience dir some dev setups use), /tmp/.node_test
    candidates = [(None, None),
                  (str(Path(__file__).resolve().parent.parent), None),
                  (None, "/root/projects/WebOllama/.node_test/node_modules"),
                  (None, "/tmp/.node_test/node_modules")]
    for cwd, node_path in candidates:
        env = dict(os.environ)
        if node_path:
            env["NODE_PATH"] = node_path
        try:
            r = subprocess.run(["node", "-e", probe], capture_output=True, text=True,
                               timeout=15, cwd=cwd, env=env)
        except subprocess.TimeoutExpired:
            continue
        if r.returncode == 0 and "ok" in r.stdout:
            return True
    return False


HAS_JS_MARKDOWN_STACK = _node_with_jsdom()


@pytest.mark.skipif(not HAS_JS_MARKDOWN_STACK, reason="node or jsdom not available")
def test_markdown_bridge_and_security():
    harness = HARNESS.replace("process.argv[2]", json.dumps(str(MARKED_JS)))
    harness = harness.replace("process.argv[3]", json.dumps(str(PURIFY_JS)))
    harness = harness.replace("process.argv[4]", json.dumps(str(MD_JS)))
    harness = harness.replace("process.argv[5]", json.dumps(str(CHAT_JS)))
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(harness)
        path = f.name
    try:
        env = dict(os.environ)
        # let the harness resolve jsdom from the convenience install dirs
        for p in ("/root/projects/WebOllama/.node_test/node_modules",
                  "/tmp/.node_test/node_modules"):
            if Path(p).exists():
                env["NODE_PATH"] = env.get("NODE_PATH", "") + (":" if env.get("NODE_PATH") else "") + p
        r = subprocess.run(["node", path], capture_output=True, text=True, timeout=60, env=env)
        assert r.returncode == 0, f"markdown bridge tests failed:\n{r.stdout}\n{r.stderr}"
        assert "FAIL" not in r.stdout, r.stdout
    finally:
        Path(path).unlink(missing_ok=True)


def test_vendored_libs_present_and_index_includes_them():
    """Vendor files must exist and be loaded BEFORE md.js/chat.js in index.html."""
    assert MARKED_JS.exists() and MARKED_JS.stat().st_size > 10_000
    assert PURIFY_JS.exists() and PURIFY_JS.stat().st_size > 5_000
    html = (Path(__file__).resolve().parent.parent / "webui" / "static" / "index.html").read_text()
    i_chart = html.index("chart.umd.min.js")
    i_marked = html.index("vendor/marked.min.js")
    i_purify = html.index("vendor/purify.min.js")
    i_md = html.index("/js/md.js")
    i_chat = html.index("pages/chat.js")
    assert i_chart < i_marked < i_purify < i_md < i_chat, "script order wrong"


def test_md_bridge_never_marks_raw_html_safe():
    """The bridge must sanitize even if marked is present but DOMPurify is not."""
    src = MD_JS.read_text()
    assert "escapeHtml" in src
    assert "available()" in src
    assert "DOMPurify.sanitize" in src
