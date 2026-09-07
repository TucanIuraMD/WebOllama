/* Markdown rendering bridge for chat replies.
 *
 * Pipeline: markdown text → marked.parse() → DOMPurify.sanitize() → HTML.
 * Both libraries are vendored (see vendor/); if either is missing the
 * bridge degrades gracefully to escaped plain text, so the chat page
 * never breaks and never injects unsanitized HTML.
 *
 * Security notes:
 * - marked does NOT sanitize raw HTML in markdown; DOMPurify strips
 *   scripts, event handlers (onerror=...), javascript: URLs and other
 *   dangerous markup while keeping safe formatting (headers, lists,
 *   code blocks with language- class, links, tables, quotes).
 * - Code blocks get language-* classes for future highlight.js support.
 */
(function () {
  "use strict";

  function available() {
    // DOMPurify exports a function with attached methods in some UMD
    // environments (function factory), a plain object in others — accept both.
    const dp = window.DOMPurify;
    const dpOk = dp && (typeof dp === "object" || typeof dp === "function") &&
      typeof dp.sanitize === "function" && dp.isSupported !== false;
    return typeof window.marked === "object" &&
      typeof window.marked.parse === "function" && dpOk;
  }

  /* marked config: GFM (tables, strikethrough, task lists), \n → <br>,
   * code blocks carry language-xxx classes. */
  function configure() {
    if (window.marked.setOptions) {
      window.marked.setOptions({
        gfm: true,
        breaks: true,
        pedantic: false,
      });
    }
  }
  let configured = false;

  function escapeHtml(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  function renderMarkdown(text) {
    const s = String(text == null ? "" : text);
    if (!s) return "";
    if (!available()) {
      // Degrade safely: escaped text with newlines preserved (pre-wrap CSS
      // handles the layout). Never fall back to unsanitized marked output.
      return escapeHtml(s);
    }
    if (!configured) { configure(); configured = true; }
    let html;
    try {
      html = window.marked.parse(s);
    } catch {
      return escapeHtml(s);
    }
    try {
      return window.DOMPurify.sanitize(html, {
        // keep formatting tags; DOMPurify's default allow-list already
        // covers them and strips scripts/handlers/js-URLs.
        USE_PROFILES: { html: true },
        FORBID_TAGS: ["style", "form", "input", "button", "iframe", "object", "embed"],
        FORBID_ATTR: ["style"],
      });
    } catch {
      return escapeHtml(s);
    }
  }

  window.renderMarkdown = renderMarkdown;
  // exposed for tests / debugging
  window.__markdownAvailable = available;
})();
