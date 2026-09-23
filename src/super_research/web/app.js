import { getKeys } from "./store.js";
import { renderNew } from "./views/new.js";
import { renderKeys } from "./views/keys.js";
import { renderRuns } from "./views/runs.js";
import { renderRun } from "./views/run.js";

const app = document.getElementById("app");
let cleanup = null;

function route() {
  const [name = "", ...rest] = location.hash.replace(/^#\/?/, "").split("/");
  const arg = decodeURIComponent(rest.join("/"));
  switch (name) {
    case "keys": return { name, view: renderKeys };
    case "runs": return { name, view: renderRuns };
    case "run": return { name: "runs", view: renderRun, arg };
    default: return { name: "new", view: renderNew };
  }
}

function topbar(current) {
  const k = getKeys();
  const ready = !!(k.opencode && k.typesafe);
  const link = (href, name, text) => `<a class="label" href="${href}" ${current === name ? 'aria-current="page"' : ""}>${text}</a>`;
  return `
    <header class="topbar">
      <a class="topbar__name" href="#/">Super Research</a>
      <nav aria-label="Main">
        ${link("#/", "new", "New Run")}
        ${link("#/runs", "runs", "Runs")}
        ${link("#/keys", "keys", `<span class="keydot ${ready ? "keydot--on" : ""}" aria-hidden="true"></span>${ready ? "Keys Set" : "Add Keys"}`)}
      </nav>
    </header>`;
}

function footer() {
  return `
    <footer class="footer">
      <div class="shell">
        <p class="footer__name">Super Research</p>
        <p class="caption">Jev decides, Needle extracts, Tavily and SearXNG search, one OpenCode Go call writes the report from the knowledge tree. Your keys stay in your browser and travel only with the run you start.</p>
      </div>
    </footer>`;
}

export function render() {
  if (typeof cleanup === "function") cleanup();
  cleanup = null;
  const r = route();
  app.innerHTML = `<div class="shell">${topbar(r.name)}<main id="main"></main></div>${footer()}`;
  cleanup = r.view(app.querySelector("#main"), r.arg) || null;
  window.scrollTo(0, 0);
}

export function refreshTopbar() {
  const old = app.querySelector(".topbar");
  if (old) old.outerHTML = topbar(route().name);
}

window.addEventListener("hashchange", render);
render();
