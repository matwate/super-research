// Small shared helpers.

export function esc(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

export function safeUrl(url) {
  try {
    const u = new URL(url);
    return u.protocol === "https:" || u.protocol === "http:" ? u.href : "#";
  } catch {
    return "#";
  }
}

export function host(url) {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return "";
  }
}

export const fmt = {
  p: (v) => (v == null ? "—" : Number(v).toFixed(2)),
  usd: (v) => (v == null ? "—" : `$${Number(v) < 0.01 ? Number(v).toFixed(4) : Number(v).toFixed(3)}`),
  secs: (v) => (v == null ? "—" : v < 90 ? `${Math.round(v)}s` : `${Math.floor(v / 60)}m ${Math.round(v % 60)}s`),
};

let toastTimer;
export function toast(message) {
  let el = document.querySelector(".toast");
  if (!el) {
    el = Object.assign(document.createElement("div"), { className: "toast" });
    el.setAttribute("role", "status");
    document.body.append(el);
  }
  el.textContent = message;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.remove(), 2600);
}

// Markdown via the vendored marked + DOMPurify (static/vendor); plain text if they fail.
let mdLib;
export async function markdown(text) {
  try {
    mdLib ||= Promise.all([import("./vendor/marked.esm.js"), import("./vendor/purify.es.js")]);
    const [{ marked }, { default: DOMPurify }] = await mdLib;
    return DOMPurify.sanitize(marked.parse(unfence(text || ""), { gfm: true }), { FORBID_TAGS: ["img", "style", "iframe", "form", "input"] });
  } catch (e) {
    console.error("markdown renderer failed to load", e);
    mdLib = null;
    return `<pre class="pre">${esc(text)}</pre>`;
  }
}

// Some models wrap the whole answer in ```markdown ... ```; older reports on disk may too.
export function unfence(text) {
  const m = text.trim().match(/^```(?:markdown|md)?[ \t]*\n([\s\S]*?)\n```$/i);
  return m ? m[1] : text;
}

export function debounce(fn, ms) {
  let t;
  return (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
}
