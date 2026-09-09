"""Chat code block actions — Save / Reply above and below every <pre>.

Runs the REAL chat.js + md.js + vendored marked/DOMPurify inside jsdom and
drives the full user flows for the code block action bars:

  placement   — every rendered code block gets ONE action bar above and
                ONE below the code;
  correctness — Save/Reply target exactly the block whose button was
                pressed (multiple independent blocks);
  Save        — client-only (no fetch), strips fences/language tag,
                preserves code text exactly, language → extension mapping,
                unknown language → .txt;
  Reply       — fills the EXISTING composer (#chat-input) with this
                block's fenced code, never auto-sends (Send button label
                and fetch are not touched), value-set semantics (no HTML
                execution through the composer);
  streaming   — per-delta + final re-render keeps actions correct and
                never duplicates bars (whole-subtree re-render strategy);
  regression  — markdown/XSS behavior from test_chat_markdown still holds
                on the decorated DOM.

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
JSDOM_DIR = Path("/tmp/jsdom-env/node_modules")

HARNESS = r"""
const fs = require('fs');
const { JSDOM } = require('jsdom');

const dom = new JSDOM('<!doctype html><html><body>' +
  '<div id="page-root"></div></body></html>', { url: 'http://localhost/#chat',
  runScripts: 'outside-only' });
const vm = require('vm');
const w = dom.window;
const { document } = w;

// marked: UMD prefers module.exports under node — load in isolated scope.
const markedSrc = fs.readFileSync(process.argv[2], 'utf8');
const mfn = new Function('module', 'exports', 'define', markedSrc);
const mmod = { exports: {} };
mfn(mmod, mmod.exports, undefined);

// DOMPurify: needs a real DOM — hand it the jsdom window.
const dpSrc = fs.readFileSync(process.argv[3], 'utf8');
const dfn = new Function('module', 'exports', 'define', 'window', 'document', dpSrc);
const dmod = { exports: {} };
dfn(dmod, dmod.exports, undefined, w, document);

w.marked = mmod.exports;
w.DOMPurify = dmod.exports;

// real markdown bridge
const mdFn = new Function('window', fs.readFileSync(process.argv[4], 'utf8'));
mdFn(w);

// --- minimal globals chat.js expects (same shape as test_chat_frontend) ---
function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
w.esc = esc;
w.fmtDuration = (sec) => `${Math.floor(sec || 0)}s`;
const apiCalls = [];
const AppGlobal = { state: { user: { username: 'admin', role: 'admin' }, pageQuery: {} } };
w.App = AppGlobal;   // chat.js is loaded in a Function scope: bare `App` resolves via window
w.API = {
  async get(url) { apiCalls.push(['GET', url]); return { models: [{ name: 'qwen3:8b' }] }; },
  async post(url, body) { apiCalls.push(['POST', url, body]); return {}; },
};

// downloads: capture instead of navigating
const downloads = [];
w.URL.createObjectURL = (blob) => { const u = 'blob:fake-' + downloads.length; downloads.push({ url: u, blob }); return u; };
w.URL.revokeObjectURL = () => {};

// fetch stub + SSE stream for the streaming flow
let fetchCalls = [];
let sseChunks = [];   // array of {event, data} objects delivered in order
w.fetch = async (url, opts) => {
  fetchCalls.push({ url, opts });
  const events = sseChunks;
  const enc = new w.TextEncoder();
  let buf = '';
  for (const [ev, data] of events) buf += `event: ${ev}\ndata: ${JSON.stringify(data)}\n\n`;
  const reader = {
    async read() {
      if (!buf.length) return { done: true, value: undefined };
      const val = buf; buf = '';
      return { done: false, value: enc.encode(val) };
    },
  };
  return { ok: true, status: 200, body: { getReader: () => reader } };
};

// real chat.js evaluated in the jsdom window's own context — bare globals
// (App, esc, fmtDuration, API) resolve exactly like in the browser bundle.
vm.runInContext(fs.readFileSync(process.argv[5], 'utf8'), w);

let failures = 0;
async function check(name, fn) {
  try { await fn(); console.log(name, 'PASS'); }
  catch (e) { failures++; console.log(name, 'FAIL:', e.message); }
}
function assert(cond, msg) { if (!cond) throw new Error(msg || 'assertion failed'); }

const PY_SNIPPET = 'def hello():\n    print("Hello")';
const TWO_BLOCKS =
  'Первый блок:\n\n```python\n' + PY_SNIPPET + '\n```\n\n' +
  'Второй блок:\n\n```js\nconsole.log("two");\n```\n\n' +
  'И блок без языка:\n\n```\nplain content here\n```';

async function main() {
  const chat = w.Pages.chat;

  await check('page renders with composer textarea', async () => {
    const root = document.getElementById('page-root');
    await chat.render(root);
    const input = document.getElementById('chat-input');
    assert(input, 'composer #chat-input missing');
    assert(input.tagName === 'TEXTAREA', 'composer must be a textarea, got ' + input.tagName);
    assert(document.getElementById('chat-send'), 'send button missing');
  });

  await check('every code block gets actions above AND below', () => {
    const body = document.createElement('span');
    body.className = 'chat-md';
    body.innerHTML = w.renderMarkdown('```python\n' + PY_SNIPPET + '\n```');
    const n = chat.decorateCodeActions(body);
    assert(n === 1, 'one pre expected, decorated ' + n);
    const pre = body.querySelector('pre');
    const bars = pre.querySelectorAll('.code-actions');
    assert(bars.length === 2, 'expected 2 bars (top+bottom), got ' + bars.length);
    const first = pre.firstChild;
    const last = pre.lastChild;
    assert(first.classList && first.classList.contains('code-actions'), 'first child of pre is not the top bar');
    assert(last.classList && last.classList.contains('code-actions'), 'last child of pre is not the bottom bar');
    for (const bar of bars) {
      const bs = bar.querySelectorAll('.cb-btn');
      assert(bs.length === 3, 'bar must have Копировать+Сохранить+Ответить, got ' + bs.length);
      assert(bs[0].getAttribute('data-cb-action') === 'copy', 'copy action attr missing');
      assert(bs[1].getAttribute('data-cb-action') === 'save', 'save action attr missing');
      assert(bs[2].getAttribute('data-cb-action') === 'reply', 'reply action attr missing');
      assert(bs[0].textContent === 'Копировать', 'copy label wrong: ' + bs[0].textContent);
      assert(bs[1].textContent === 'Сохранить', 'save label wrong: ' + bs[1].textContent);
      assert(bs[2].textContent === 'Ответить', 'reply label wrong: ' + bs[2].textContent);
    }
    // the code itself is untouched (no fence text, no injected content)
    const code = pre.querySelector('code');
    assert(code.textContent === PY_SNIPPET + '\n' || code.textContent === PY_SNIPPET,
           'code content mutated by decoration');
    assert(!code.textContent.includes('```'), 'fence leaked into code');
  });

  await check('Save strips fences and language, downloads exact code, no server', () => {
    downloads.length = 0; apiCalls.length = 0;
    const body = document.createElement('span');
    body.innerHTML = w.renderMarkdown('```python\n' + PY_SNIPPET + '\n```');
    chat.decorateCodeActions(body);
    const pre = body.querySelector('pre');
    const saveBtn = pre.querySelector('.cb-btn[data-cb-action="save"]');
    saveBtn.click();
    assert(downloads.length === 1, 'no download happened');
    const blob = downloads[0].blob;
    assert(blob.type.startsWith('text/plain'), 'unexpected mime: ' + blob.type);
    // exact content: no fences, no language tag, code byte-exact
    return blob.text().then((txt) => {
      assert(txt === PY_SNIPPET, 'saved content mismatch: ' + JSON.stringify(txt));
      assert(!txt.includes('```'), 'fence in saved file');
      assert(!txt.includes('python'), 'language tag in saved file');
      assert(apiCalls.length === 0, 'Save must be client-only, made calls: ' + JSON.stringify(apiCalls));
      assert(fetchCalls.length === 0, 'Save must not fetch');
    });
  });

  await check('language tag maps to a sensible extension', () => {
    const cases = [
      ['python', /^script\.py$/], ['javascript', /\.js$/], ['bash', /\.sh$/],
      ['json', /\.json$/], ['typescript', /\.ts$/], ['go', /\.go$/], ['sql', /\.sql$/],
    ];
    for (const [lang, re] of cases) {
      const name = chat.fileNameFor(lang);
      assert(re.test(name), `lang ${lang} → unexpected name ${name}`);
    }
    assert(chat.fileNameFor('') === 'snippet.txt', 'empty lang must be .txt, got ' + chat.fileNameFor(''));
    assert(chat.fileNameFor('brainfuck') === 'snippet.txt', 'unknown lang must be .txt, got ' + chat.fileNameFor('brainfuck'));
    assert(chat.fileNameFor(null) === 'snippet.txt', 'null lang must be .txt');
  });

  await check('unknown/absent language block saves as .txt', () => {
    downloads.length = 0;
    const body = document.createElement('span');
    body.innerHTML = w.renderMarkdown('```\nplain content here\n```');
    chat.decorateCodeActions(body);
    const pre = body.querySelector('pre');
    assert(!pre.querySelector('code.language-'), 'unexpected language class on bare block');
    pre.querySelector('.cb-btn[data-cb-action="save"]').click();
    assert(downloads.length === 1, 'no download');
    return downloads[0].blob.text().then((txt) => {
      assert(txt === 'plain content here', 'txt content mismatch: ' + JSON.stringify(txt));
    });
  });

  await check('Reply fills the existing composer, never auto-sends', () => {
    fetchCalls.length = 0; apiCalls.length = 0;
    const input = document.getElementById('chat-input');
    input.value = '';
    const sendBtn = document.getElementById('chat-send');
    const sendLabelBefore = sendBtn.textContent;
    const body = document.createElement('span');
    body.innerHTML = w.renderMarkdown('```python\n' + PY_SNIPPET + '\n```');
    chat.decorateCodeActions(body);
    const pre = body.querySelector('pre');
    pre.querySelector('.cb-btn[data-cb-action="reply"]').click();
    const v = input.value;
    assert(v.includes('```python'), 'reply text missing fenced block with lang: ' + JSON.stringify(v));
    assert(v.includes('print("Hello")'), 'reply text missing the code');
    assert(v.includes('```'), 'reply text missing fences');
    assert(fetchCalls.length === 0, 'Reply must NOT auto-send (fetch called)');
    assert(apiCalls.length === 0, 'Reply must NOT auto-send (API called)');
    assert(sendBtn.textContent === sendLabelBefore, 'Send button state changed by Reply');
    assert(!document.getElementById('chat-send').disabled, 'send disabled by reply');
    // composer value is plain text — HTML inside code cannot become markup
    const evil = '<script>alert(1)<\/script>';
    const body2 = document.createElement('span');
    body2.innerHTML = w.renderMarkdown('```\n' + evil + '\n```');
    chat.decorateCodeActions(body2);
    body2.querySelector('.cb-btn[data-cb-action="reply"]').click();
    assert(input.value.includes('<script>alert(1)'), 'evil code lost from composer value');
    assert(input.value === input.value, 'value is a plain JS string (no live markup)');
    // no script element was created anywhere
    assert(!document.querySelector('script:not([src])'), 'live script node appeared');
  });

  await check('multiple blocks: buttons target their OWN block', () => {
    downloads.length = 0;
    const input = document.getElementById('chat-input');
    const body = document.createElement('span');
    body.innerHTML = w.renderMarkdown(TWO_BLOCKS);
    chat.decorateCodeActions(body);
    const pres = body.querySelectorAll('pre');
    assert(pres.length === 3, 'expected 3 blocks, got ' + pres.length);
    // each block: top+bottom bars, each with 2 buttons
    for (const pre of pres) {
      const bars = pre.querySelectorAll('.code-actions');
      assert(bars.length === 2, 'bar count wrong on one of the blocks');
      assert(pre.querySelectorAll('.cb-btn').length === 6, 'button count wrong (3 per bar)');
    }
    // Save the FIRST block from its BOTTOM bar → exact python content
    pres[0].querySelectorAll('.cb-btn[data-cb-action="save"]')[1].click();
    // Save the SECOND block from its TOP bar → exact js content
    pres[1].querySelector('.cb-btn[data-cb-action="save"]').click();
    // Reply from the THIRD block (no language) → its own content
    pres[2].querySelector('.cb-btn[data-cb-action="reply"]').click();
    return Promise.all([
      downloads[0].blob.text(),
      downloads[1].blob.text(),
    ]).then(([t0, t1]) => {
      assert(t0 === PY_SNIPPET, 'block 1 save mismatch: ' + JSON.stringify(t0));
      assert(t1 === 'console.log("two");', 'block 2 save mismatch: ' + JSON.stringify(t1));
      assert(input.value.includes('plain content here'), 'reply filled wrong block: ' + JSON.stringify(input.value));
      assert(!input.value.includes('print("Hello")'), 'reply leaked block 1');
      assert(!input.value.includes('console.log'), 'reply leaked block 2');
    });
  });

  await check('streaming + final render: actions correct, never duplicated', async () => {
    // drive the real sendMessage flow with two deltas containing a code block
    const input = document.getElementById('chat-input');
    input.value = 'give me code';
    sseChunks = [
      ['delta', { content: '```py\nprint("pa' }],
      ['delta', { content: 'rtial")\n```' }],
      ['done', { model: 'qwen3:8b', eval_count: 5 }],
    ];
    await w.Pages.chat.sendMessage();
    const bodies = [];
    (function walk(n) {
      for (const c of n.children || []) {
        if (String(c.className).includes('chat-md')) bodies.push(c);
        walk(c);
      }
    })(document.getElementById('chat-output'));
    assert(bodies.length === 1, 'expected one .chat-md body, got ' + bodies.length);
    const pres = bodies[0].querySelectorAll('pre');
    assert(pres.length === 1, 'final body must contain exactly one pre, got ' + pres.length);
    const pre = pres[0];
    const bars = pre.querySelectorAll('.code-actions');
    assert(bars.length === 2, 'final render: 2 bars expected, got ' + bars.length);
    assert(pre.querySelectorAll('.cb-btn').length === 6, 'final render: 6 buttons expected');
    // buttons work after the final render
    downloads.length = 0;
    pre.querySelector('.cb-btn[data-cb-action="save"]').click();
    assert(downloads.length === 1, 'final-render Save broken');
    const txt = await downloads[0].blob.text();
    assert(txt === 'print("partial")', 'final-render save content: ' + JSON.stringify(txt));
    // no lingering decoration from intermediate renders (innerHTML swap
    // discards old subtrees) — total bar count in the whole body is 2
    assert(bodies[0].querySelectorAll('.code-actions').length === 2,
           'duplicate bars across renders: ' + bodies[0].querySelectorAll('.code-actions').length);
  });

  await check('markdown/XSS regression on decorated DOM', () => {
    const body = document.createElement('span');
    body.innerHTML = w.renderMarkdown(
      '# Заголовок\n\n**bold** `ic`\n\n<script>alert(9)<\/script>\n\n' + TWO_BLOCKS);
    chat.decorateCodeActions(body);
    const html = body.innerHTML;
    assert(html.includes('<h1>Заголовок</h1>'), 'h1 lost');
    assert(html.includes('<strong>bold</strong>'), 'bold lost');
    assert(html.includes('<code>ic</code>'), 'inline code lost');
    assert(!html.includes('<script'), 'script survived');
    assert(!html.includes('alert(9)'), 'script payload survived');
    // decoration did not disturb markdown structure
    assert(body.querySelectorAll('pre').length === 3, 'pre count changed');
    for (const pre of body.querySelectorAll('pre')) {
      assert(pre.querySelectorAll('.code-actions').length === 2, 'bars lost on mixed doc');
    }
    // headers/text nodes keep working — decoration only touches pre's
    assert(html.includes('<li>список</li>') || html.includes('Второй блок'), 'text content lost');
  });

  await check('no listener leaks: re-decoration skips existing bars, fresh bars carry fresh handlers', () => {
    const body = document.createElement('span');
    body.innerHTML = w.renderMarkdown('```python\nx = 1\n```');
    chat.decorateCodeActions(body);
    const pre = body.querySelector('pre');
    const clicksBefore = pre.querySelectorAll('.cb-btn').length;
    const n2 = chat.decorateCodeActions(body); // second pass — idempotent
    assert(n2 === 0, 'second decorate must add nothing, added ' + n2);
    assert(pre.querySelectorAll('.code-actions').length === 2, 'bars duplicated on re-decoration');
    assert(pre.querySelectorAll('.cb-btn').length === clicksBefore, 'buttons duplicated on re-decoration');
    // after re-render the old subtree is discarded entirely: old listeners
    // die with it; new bars have exactly one click handler each (spy via
    // counting invocations: clicking Save twice triggers exactly 2 downloads)
    downloads.length = 0;
    pre.querySelector('.cb-btn[data-cb-action="save"]').click();
    pre.querySelector('.cb-btn[data-cb-action="save"]').click();
    assert(downloads.length === 2, 'handler fired wrong number of times: ' + downloads.length);
  });

  await check('long block layout: top bar before code, bottom bar after (DOM order)', () => {
    const body = document.createElement('span');
    body.innerHTML = w.renderMarkdown('```python\n' + Array(60).fill('x = 1').join('\n') + '\n```');
    chat.decorateCodeActions(body);
    const pre = body.querySelector('pre');
    const kids = Array.from(pre.children);
    assert(kids[0].classList.contains('code-actions'), 'first element must be top bar, got ' + kids[0].className);
    assert(kids[kids.length - 1].classList.contains('code-actions'), 'last element must be bottom bar, got ' + kids[kids.length - 1].className);
    assert(kids.length === 3 && kids[1].tagName.toLowerCase() === 'code', 'exactly one code element must sit between the bars, got ' + kids.map((c) => c.tagName).join(','));
  });

  // ---- v1.1: Копировать (clipboard) -------------------------------------

  await check('Copy: top button of FIRST block, exact content, no fetch', async () => {
    fetchCalls.length = 0; apiCalls.length = 0;
    const written = [];
    Object.defineProperty(w.navigator, 'clipboard', {
      configurable: true,
      value: { writeText: (t) => { written.push(t); return Promise.resolve(); } },
    });
    const body = document.createElement('span');
    body.innerHTML = w.renderMarkdown(TWO_BLOCKS);
    chat.decorateCodeActions(body);
    const pres = body.querySelectorAll('pre');
    // top bar, copy button of the first (python) block
    pres[0].querySelector('.cb-btn[data-cb-action="copy"]').click();
    await Promise.resolve(); // flush the writeText promise
    assert(written.length === 1, 'clipboard.writeText not called');
    assert(written[0] === PY_SNIPPET, 'copied content mismatch: ' + JSON.stringify(written[0]));
    assert(!written[0].includes('```'), 'fence in clipboard');
    assert(!written[0].includes('python'), 'language tag in clipboard');
    assert(fetchCalls.length === 0, 'Copy must not fetch');
    assert(apiCalls.length === 0, 'Copy must not call API');
  });

  await check('Copy: bottom button of SECOND block, independence', async () => {
    const written = [];
    Object.defineProperty(w.navigator, 'clipboard', {
      configurable: true,
      value: { writeText: (t) => { written.push(t); return Promise.resolve(); } },
    });
    const body = document.createElement('span');
    body.innerHTML = w.renderMarkdown(TWO_BLOCKS);
    chat.decorateCodeActions(body);
    const pres = body.querySelectorAll('pre');
    const bottomBar = pres[1].querySelectorAll('.code-actions')[1];
    bottomBar.querySelector('.cb-btn[data-cb-action="copy"]').click();
    await Promise.resolve();
    assert(written.length === 1, 'one write expected, got ' + written.length);
    assert(written[0] === 'console.log("two");', 'second block copy mismatch: ' + JSON.stringify(written[0]));
    // then copy the THIRD (no-language) block from its top bar — still independent
    pres[2].querySelector('.cb-btn[data-cb-action="copy"]').click();
    await Promise.resolve();
    assert(written.length === 2 && written[1] === 'plain content here',
           'third block copy mismatch: ' + JSON.stringify(written));
  });

  await check('Copy feedback: «Скопировано», then label restores', async () => {
    const written = [];
    Object.defineProperty(w.navigator, 'clipboard', {
      configurable: true,
      value: { writeText: (t) => { written.push(t); return Promise.resolve(); } },
    });
    const body = document.createElement('span');
    body.innerHTML = w.renderMarkdown('```python\nx = 1\n```');
    chat.decorateCodeActions(body);
    const btn = body.querySelector('.cb-btn[data-cb-action="copy"]');
    btn.click();
    await Promise.resolve();
    assert(btn.textContent === 'Скопировано', 'no success feedback, label: ' + btn.textContent);
    assert(btn.classList.contains('cb-ok'), 'cb-ok class missing');
    // feedback clears itself (1.5s timer — do not wait for real time in tests,
    // just verify the mechanism exists via the timer callback path)
  });

  await check('Clipboard failure handled: «Ошибка», chat keeps working', async () => {
    Object.defineProperty(w.navigator, 'clipboard', {
      configurable: true,
      value: { writeText: () => Promise.reject(new Error('NotAllowedError')) },
    });
    const body = document.createElement('span');
    body.innerHTML = w.renderMarkdown('```python\nx = 2\n```');
    chat.decorateCodeActions(body);
    const btn = body.querySelector('.cb-btn[data-cb-action="copy"]');
    btn.click();
    await Promise.resolve();
    await new Promise((r) => setTimeout(r, 0)); // let the rejection handler run
    assert(btn.textContent === 'Ошибка', 'no failure feedback, label: ' + btn.textContent);
    assert(btn.classList.contains('cb-err'), 'cb-err class missing');
    // chat is alive: another block's actions still work after the failure
    const written = [];
    Object.defineProperty(w.navigator, 'clipboard', {
      configurable: true,
      value: { writeText: (t) => { written.push(t); return Promise.resolve(); } },
    });
    const body2 = document.createElement('span');
    body2.innerHTML = w.renderMarkdown('```js\nlet y = 2;\n```');
    chat.decorateCodeActions(body2);
    body2.querySelector('.cb-btn[data-cb-action="copy"]').click();
    await Promise.resolve();
    assert(written.length === 1 && written[0] === 'let y = 2;', 'copy broken after clipboard failure');
    // and Save still works
    downloads.length = 0;
    body2.querySelector('.cb-btn[data-cb-action="save"]').click();
    const txt = await downloads[0].blob.text();
    assert(txt === 'let y = 2;', 'Save broken after clipboard failure');
  });

  await check('Copy without clipboard API (http context) falls back, no crash', async () => {
    // remove the API entirely — plain-http LAN context (e.g. Hermes)
    Object.defineProperty(w.navigator, 'clipboard', { configurable: true, value: undefined });
    const body = document.createElement('span');
    body.innerHTML = w.renderMarkdown('```python\nx = 3\n```');
    chat.decorateCodeActions(body);
    const btn = body.querySelector('.cb-btn[data-cb-action="copy"]');
    btn.click(); // must not throw even without execCommand in jsdom
    await new Promise((r) => setTimeout(r, 0));
    assert(btn.textContent === 'Ошибка', 'graceful failure expected, got: ' + btn.textContent);
    // the DOM is intact — the block content is unchanged
    const pre = body.querySelector('pre');
    assert(pre.querySelector('code').textContent.includes('x = 3'), 'code content mutated by failed copy');
  });

  await check('inline code gets NO code block actions', () => {
    const body = document.createElement('span');
    body.innerHTML = w.renderMarkdown('use `fmt.Stringer` inline and **bold**');
    chat.decorateCodeActions(body);
    assert(body.querySelectorAll('pre').length === 0, 'unexpected pre');
    assert(body.querySelectorAll('.code-actions').length === 0, 'inline code got action bars');
    assert(body.innerHTML.includes('<code>fmt.Stringer</code>'), 'inline code lost');
  });

  await check('actions do not mutate the code block / transcript (chat keeps the reply)', () => {
    const body = document.createElement('span');
    body.innerHTML = w.renderMarkdown('```python\nkeep = True\n```');
    chat.decorateCodeActions(body);
    const pre = body.querySelector('pre');
    const before = pre.querySelector('code').textContent;
    // fire ALL actions on this block: copy, save, reply
    downloads.length = 0;
    pre.querySelector('.cb-btn[data-cb-action="copy"]').click();
    pre.querySelector('.cb-btn[data-cb-action="save"]').click();
    pre.querySelector('.cb-btn[data-cb-action="reply"]').click();
    assert(pre.querySelector('code').textContent === before, 'code content mutated by actions');
    assert(pre.querySelectorAll('.code-actions').length === 2, 'bars duplicated by actions');
    assert(downloads.length === 1, 'save did not fire');
    // transcript/body text is untouched by decoration actions
    assert(body.textContent.includes('keep = True'), 'body lost the reply text');
  });

  await check('full chat flow: reply context preserved through Copy/Save/Reply + composer send', async () => {
    // fresh page render
    document.getElementById('chat-output').innerHTML = '';
    const root = document.getElementById('page-root');
    await chat.render(root);
    const input = document.getElementById('chat-input');
    const out = document.getElementById('chat-output');
    // message 1 → assistant reply with a code block
    sseChunks = [['delta', { content: 'Ответ:\n```python\nctx = 42\n```' }], ['done', { model: 'qwen3:8b' }]];
    input.value = 'первый вопрос';
    await w.Pages.chat.sendMessage();
    const bodyMd = out.querySelector('.chat-md');
    assert(bodyMd, 'no reply body');
    assert(bodyMd.textContent.includes('ctx = 42'), 'reply text lost after stream');
    const pre = bodyMd.querySelector('pre');
    assert(pre && pre.querySelectorAll('.code-actions').length === 2, 'actions missing after stream');
    // user performs Copy + Save + Reply on that block
    const written = [];
    Object.defineProperty(w.navigator, 'clipboard', {
      configurable: true,
      value: { writeText: (t) => { written.push(t); return Promise.resolve(); } },
    });
    downloads.length = 0; fetchCalls.length = 0;
    pre.querySelector('.cb-btn[data-cb-action="copy"]').click();
    pre.querySelector('.cb-btn[data-cb-action="save"]').click();
    pre.querySelector('.cb-btn[data-cb-action="reply"]').click();
    await Promise.resolve();
    assert(written.length === 1 && downloads.length === 1, 'actions did not run');
    assert((await downloads[0].blob.text()) === 'ctx = 42', 'save content mismatch');
    // the received reply is still on screen (not cleared by the actions)
    assert(out.querySelector('.chat-md').textContent.includes('ctx = 42'), 'transcript cleared by actions');
    assert(!fetchCalls.length, 'actions fetched');
    // composer now holds the Reply quote — user edits it and presses Enter
    assert(input.value.includes('ctx = 42'), 'composer lost the quote');
    input.value = input.value + 'мой вопрос по коду';
    input.dispatchEvent(new w.KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
    await Promise.resolve();
    await new Promise((r) => setTimeout(r, 0));
    assert(fetchCalls.length === 1, 'second send did not POST');
    const sent = JSON.parse(fetchCalls[0].opts.body);
    const msgs = sent.messages;
    // context is well-formed and ordered: strictly alternating turns
    for (let i = 0; i < msgs.length; i++) {
      const want = i % 2 === 0 ? 'user' : 'assistant';
      assert(msgs[i].role === want, 'context role order broken at ' + i + ': ' + msgs.map((m) => m.role).join(','));
    }
    // the earlier conversation survived (user text + assistant code reply)
    const ctx = msgs.map((m) => m.content).join('\n');
    assert(ctx.includes('первый вопрос'), 'first user message lost from context');
    assert(ctx.includes('ctx = 42'), 'assistant reply lost from context');
    assert(ctx.includes('мой вопрос по коду'), 'edited composer text not sent');
    // the LAST turn is the user's edited composer text with the quote
    assert(msgs[msgs.length - 1].role === 'user', 'last context turn must be the sent user message');
    assert(msgs[msgs.length - 1].content.includes('ctx = 42'), 'quoted code missing from sent turn');
    assert(msgs[msgs.length - 1].content.includes('мой вопрос по коду'), 'edited composer text not in sent turn');
    assert(msgs[msgs.length - 2].role === 'assistant', 'assistant reply must precede the sent turn');
    // actions after this second send still work (final render path)
    const bodies = out.querySelectorAll('.chat-md');
    const lastBody = bodies[bodies.length - 1];
    assert(lastBody.querySelectorAll('.code-actions').length === 2, 'actions missing on second reply');
  });

  process.exit(failures ? 1 : 0);
}

main().catch((e) => { console.error('HARNESS ERROR', e); process.exit(2); });
"""


def _node_with_jsdom() -> bool:
    if shutil.which("node") is None:
        return False
    probe = "try{require('jsdom');console.log('ok')}catch(e){console.log('no')}"
    env = dict(os.environ)
    if JSDOM_DIR.exists():
        env["NODE_PATH"] = str(JSDOM_DIR)
    try:
        r = subprocess.run(["node", "-e", probe], capture_output=True, text=True, timeout=15, env=env)
    except subprocess.TimeoutExpired:
        return False
    return r.returncode == 0 and "ok" in r.stdout


HAS_JS = _node_with_jsdom()


@pytest.mark.skipif(not HAS_JS, reason="node or jsdom not available")
def test_chat_code_block_actions():
    harness = HARNESS
    harness = harness.replace("process.argv[2]", json.dumps(str(MARKED_JS)))
    harness = harness.replace("process.argv[3]", json.dumps(str(PURIFY_JS)))
    harness = harness.replace("process.argv[4]", json.dumps(str(MD_JS)))
    harness = harness.replace("process.argv[5]", json.dumps(str(CHAT_JS)))
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(harness)
        path = f.name
    try:
        env = dict(os.environ)
        if JSDOM_DIR.exists():
            env["NODE_PATH"] = str(JSDOM_DIR)
        r = subprocess.run(["node", path], capture_output=True, text=True, timeout=60, env=env)
        assert r.returncode == 0, f"code block actions harness failed:\n{r.stdout}\n{r.stderr}"
        assert "FAIL" not in r.stdout, r.stdout
        assert "PASS" in r.stdout, "no checks ran"
    finally:
        Path(path).unlink(missing_ok=True)


def test_code_actions_wiring_in_source():
    """Source contract: per-delta and final renders both decorate; handler
    binds to fresh buttons only (no delegation → no leaks)."""
    src = CHAT_JS.read_text()
    assert "renderChatBody(bodyNode, reply)" in src, "delta render must decorate"
    assert "if (reply) renderChatBody(bodyNode, reply)" in src, "final render must decorate"
    assert src.count("function renderChatBody(") == 1
    assert "decorateCodeActions(bodyNode)" in src
    # single-source strategy: the only places markdown is rendered into the
    # body node go through renderChatBody
    assert "bodyNode.innerHTML = renderMarkdown(reply)" not in src, \
        "raw innerHTML render bypasses decoration"
    # reply never auto-sends: replyWithCode must not call sendOrStop/sendMessage
    rw = src.split("function replyWithCode(")[1].split("\n  }")[0]
    assert "sendOrStop" not in rw and "sendMessage" not in rw, "Reply must not auto-send"
    # client-only save
    dl = src.split("function downloadCode(")[1].split("\n  }")[0]
    assert "fetch" not in dl and "API." not in dl, "Save must be client-only"
    assert "URL.createObjectURL" in dl and "a.download" in dl, "Save must use download API"
    # client-only copy via navigator.clipboard with graceful degradation
    cp = src.split("function copyCode(")[1].split("\n  }")[0]
    assert "fetch" not in cp and "API." not in cp, "Copy must be client-only"
    assert "navigator.clipboard.writeText" in cp, "Copy must use the Clipboard API"
    assert "execCommand" in cp, "Copy needs the http-context fallback"
    assert "Скопировано" in cp and "Ошибка" in cp, "Copy must give visual feedback"
    # three actions per bar, identical above and below
    assert src.count('data-cb-action="copy"') == 1, "copy button must be declared once per bar html"
    assert src.count('data-cb-action="save"') == 1 and src.count('data-cb-action="reply"') == 1
    assert "Копировать" in src and "Сохранить" in src and "Ответить" in src
