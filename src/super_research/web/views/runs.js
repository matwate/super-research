import { api } from "../api.js";
import { esc, fmt } from "../util.js";

export function renderRuns(main) {
  main.innerHTML = `
    <div class="hero-block">
      <h1 class="spot-hero">Every<br>tree so<br>far.</h1>
    </div>
    <section class="spot-section">
      <span class="spot-label">Runs</span>
      <div id="list"><p class="body">Loading…</p></div>
    </section>`;
  api.runs().then((runs) => {
    const box = main.querySelector("#list");
    if (!runs.length) {
      box.innerHTML = `<p class="body">No runs yet. <a href="#/">Start one</a>.</p>`;
      return;
    }
    box.innerHTML = `<ul class="runs">${runs.map((r) => `
      <li>
        <div>
          <a href="#/run/${encodeURIComponent(r.id)}">${esc(r.topic || r.id)}</a>
          <div class="meta muted">${r.status === "running" ? "Growing now" : [
            `${r.sources ?? 0} sources`,
            r.cost_usd != null ? fmt.usd(r.cost_usd) : null,
            r.seconds != null ? fmt.secs(r.seconds) : null,
            r.status === "error" ? "Failed" : r.has_report ? "Report" : "No report",
            r.stop_reason,
          ].filter(Boolean).map(esc).join(" · ")}</div>
        </div>
        <span class="meta">${esc(new Date(r.mtime * 1000).toLocaleString())}</span>
      </li>`).join("")}</ul>`;
  }).catch((e) => {
    main.querySelector("#list").innerHTML = `<p class="notice notice--error">${esc(e.message)}</p>`;
  });
}
