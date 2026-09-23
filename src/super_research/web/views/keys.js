import { api } from "../api.js";
import { getKeys, setKeys, keysRemembered } from "../store.js";
import { esc, toast } from "../util.js";
import { refreshTopbar } from "../app.js";

const FIELDS = [
  {
    id: "opencode",
    name: "OpenCode Go",
    need: "Required",
    what: "Drafts the seed queries with the flash model and writes the report. One report call per pass, usually cents.",
    link: "https://opencode.ai/docs/go",
  },
  {
    id: "typesafe",
    name: "TypeSafe (Jev)",
    need: "Required",
    what: "Jev makes every keep or skip decision: queries, results, pages, links and concepts. About $0.04 per million input tokens.",
    link: null,
  },
  {
    id: "tavily",
    name: "Tavily",
    need: "Optional, recommended",
    what: "Search with page text included, plus the extract fallback for bot walls. Without it the run uses SearXNG only.",
    link: "https://app.tavily.com",
  },
];

export function renderKeys(main) {
  const keys = getKeys();
  main.innerHTML = `
    <div class="hero-block">
      <h1 class="spot-hero">Bring<br>your own<br>keys.</h1>
    </div>
    <div class="stack">
      <section class="spot-section">
        <span class="spot-label">Keys</span>
        <p class="lead">Keys stay in this browser. A key goes to the server only in the request that starts your run,
          <span class="spot-mark">and the server holds it in memory for that run alone.</span></p>
        <p class="body">It never reaches <code>run.json</code>, the logs or the reports folder.</p>
        <form id="keys-form" autocomplete="off">
          ${FIELDS.map(
            (f) => `
            <div class="field">
              <label class="label" for="k-${f.id}">${esc(f.name)} · ${esc(f.need)}</label>
              <input class="input input--mono" id="k-${f.id}" name="${f.id}" type="password" spellcheck="false" value="${esc(keys[f.id] || "")}" placeholder="Paste key">
              <p class="caption">${esc(f.what)} ${f.link ? `<a href="${f.link}" target="_blank" rel="noopener noreferrer">Get a key</a>` : ""} <span class="meta" id="srv-${f.id}"></span></p>
            </div>`
          ).join("")}
          <label class="check caption" style="margin-bottom:var(--space-3)">
            <input type="checkbox" id="remember" ${keysRemembered() ? "checked" : ""}>
            <span>Remember on this device. Leave it off on a shared computer: the keys then last only for this tab.</span>
          </label>
          <div class="row">
            <button class="spot-button spot-button--solid" type="submit">Save Keys</button>
            <button class="spot-button" type="button" id="forget">Forget Keys</button>
          </div>
        </form>
      </section>
    </div>`;

  api.meta().then((m) => {
    for (const f of FIELDS) {
      if (m.server_keys[f.id]) main.querySelector(`#srv-${f.id}`).textContent = "· Server key available as fallback";
    }
  }).catch(() => {});

  main.querySelector("#keys-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const data = Object.fromEntries(new FormData(e.target));
    setKeys(data, main.querySelector("#remember").checked);
    refreshTopbar();
    toast("Keys saved");
    if (sessionStorage.getItem("sr.returnTo")) {
      location.hash = sessionStorage.getItem("sr.returnTo");
      sessionStorage.removeItem("sr.returnTo");
    }
  });
  main.querySelector("#forget").addEventListener("click", () => {
    setKeys({}, false);
    for (const f of FIELDS) main.querySelector(`#k-${f.id}`).value = "";
    main.querySelector("#remember").checked = false;
    refreshTopbar();
    toast("Keys removed from this browser");
  });
}
