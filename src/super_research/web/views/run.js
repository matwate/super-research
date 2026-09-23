import { api } from "../api.js";
import { esc, safeUrl, host, fmt, markdown, toast } from "../util.js";

const KEPT = { root: null, query: "ran", source: "scraped", concept: "promoted" };
const VIA = {
  topic: "topic", llm_seed: "drafter seed", needle_seed: "template seed", needle_concept: "named concept",
  problem_concept: "problem phrase", search: "search result", link: "followed link", extract: "extracted",
};
const GATE = { query: "Query gate", source: "Relevance / delve gate", concept: "Concept gate" };

export function renderRun(main, runId) {
  const nodes = new Map();
  const collapsed = new Set();
  const fresh = new Map(); // node id -> time it appeared, for a short highlight
  let info = null; // GET /api/runs/:id
  let progress = null;
  let events = [];
  let selected = null;
  let selectedSig = "";
  let mode = sessionStorage.getItem("sr.runMode") || "tree";
  let showAll = false;
  let es = null;
  let reportHtml = null;
  let closed = false;

  main.innerHTML = `<p class="body" style="padding:var(--space-5) 0">Loading…</p>`;

  // ---------- data ----------
  // Children by parent id, rebuilt once per change instead of scanning every node per node.
  let index = null;
  function kids(id) {
    if (!index) {
      index = new Map();
      for (const n of nodes.values()) {
        if (!visible(n)) continue;
        if (!index.has(n.parent)) index.set(n.parent, []);
        index.get(n.parent).push(n);
      }
      for (const list of index.values()) list.sort((a, b) => order(a) - order(b));
    }
    return index.get(id) || [];
  }
  function order(n) {
    return Number(n.id.slice(1));
  }
  function visible(n) {
    return showAll || n.kind === "root" || n.status === KEPT[n.kind];
  }
  function root() {
    for (const n of nodes.values()) if (n.kind === "root") return n;
    return null;
  }
  function lineage(n) {
    const chain = [];
    for (let p = nodes.get(n.parent); p; p = nodes.get(p.parent)) chain.unshift(p);
    return chain;
  }
  function counts() {
    const c = { query: 0, source: 0, concept: 0, rejected: 0, queued: 0 };
    for (const n of nodes.values()) {
      if (n.kind !== "root" && n.status === KEPT[n.kind]) c[n.kind]++;
      if (n.status === "skipped") c.rejected++;
      if (n.status === "queued") c.queued++;
    }
    return c;
  }
  function merge(list, full) {
    index = null;
    if (full) nodes.clear();
    const now = Date.now();
    for (const n of list) {
      if (!nodes.has(n.id) && !full) fresh.set(n.id, now);
      nodes.set(n.id, n);
    }
  }

  // ---------- layout ----------
  function frame() {
    const topic = info.topic || root()?.label || runId;
    main.innerHTML = `
      <div class="hero-block hero-block--tight">
        <span class="label" style="display:block;margin-bottom:var(--space-3)">Knowledge Tree</span>
        <h1 class="spot-hero ${topic.length > 32 ? "spot-hero--3" : "spot-hero--2"}">${esc(topic)}</h1>
        <div class="stage" id="stage" style="margin-top:var(--space-4)"></div>
      </div>
      <div class="grid-2">
        <div class="stats" id="stats"></div>
        <div class="budgets" id="budgets"></div>
      </div>
      <div id="alert"></div>
      <div class="toolbar" role="toolbar" aria-label="Tree tools">
        <div class="seg" role="group" aria-label="View">
          ${[["tree", "Outline"], ["map", "Map"], ["report", "Report"], ["context", "Template"]].map(([m, l]) => `<button class="spot-button" type="button" data-mode="${m}">${l}</button>`).join("")}
        </div>
        <label class="check caption"><input type="checkbox" id="show-all"><span>Show pruned</span></label>
        <button class="spot-button" type="button" id="fold">Fold Sources</button>
        <button class="spot-button" type="button" id="open">Open All</button>
        <span class="toolbar__spacer"></span>
        <button class="spot-button spot-button--solid" type="button" id="cancel" hidden>Stop Run</button>
      </div>
      <div class="workspace">
        <div id="canvas"></div>
        <aside class="workspace__detail" id="detail"></aside>
      </div>
      <section class="spot-section" style="margin-top:var(--space-5)">
        <span class="spot-label">Wire</span>
        <ol class="wire" id="wire"></ol>
      </section>`;
    main.querySelector(".toolbar").addEventListener("click", onToolbar);
    main.querySelector("#show-all").addEventListener("change", (e) => { showAll = e.target.checked; index = null; drawCanvas(); });
    main.querySelector("#canvas").addEventListener("click", onCanvas);
    main.querySelector("#canvas").addEventListener("keydown", onKey);
    main.querySelector("#detail").addEventListener("click", onDetail);
  }

  function drawHeader() {
    const p = progress;
    const s = info.summary;
    const live = p?.status === "running";
    const stage = live ? p.stage : s ? (s.error ? "Failed" : "Done") : p?.status || "Done";
    const elapsed = live ? p.elapsed : s?.timings?.total_s;
    main.querySelector("#stage").className = `stage ${live ? "running" : ""}`;
    main.querySelector("#stage").innerHTML = `<span class="pulse" aria-hidden="true"></span>
      <span class="label">${esc(stage)}</span>
      <span class="meta muted">${fmt.secs(elapsed)}${live ? ` of ${fmt.secs(p.max_seconds)}` : ""}${(p?.stop_reason || s?.stop_reason) ? ` · stopped: ${esc(p?.stop_reason || s?.stop_reason)}` : ""}</span>`;

    const c = counts();
    const jev = p?.jev || (s && { calls: s.jev.calls, usd: s.jev.cost_usd });
    const tav = p?.tavily || (s && { credits: s.search.tavily_credits, usd: s.search.tavily_usd });
    const stat = (n, l) => `<div><span class="stat__n">${n}</span><span class="meta">${l}</span></div>`;
    main.querySelector("#stats").innerHTML = [
      stat(c.query, "Queries ran"),
      stat(c.source, "Sources read"),
      stat(c.concept, "Concepts grown"),
      stat(live ? p.frontier ?? 0 : c.rejected, live ? "In frontier" : "Rejected by Jev"),
      stat(jev ? fmt.usd(jev.usd) : "—", `Jev · ${jev?.calls ?? 0} calls`),
      stat(tav ? fmt.usd(tav.usd) : "—", `Tavily · ${tav?.credits ?? 0} credits`),
      s ? stat(s.cost_usd_total != null ? fmt.usd(s.cost_usd_total) : "—", "Total cost") : "",
      s?.tree?.delve_precision != null ? stat(fmt.p(s.tree.delve_precision), "Delve precision") : "",
    ].join("");

    const depth = p?.depth || (s && s.tree.pages_per_depth_spent.map((spent, i) => ({ spent, budget: s.tree.pages_per_depth_budget[i] })));
    main.querySelector("#budgets").innerHTML = depth
      ? `<span class="label">Pages Per Depth</span>` + depth.map((d, i) => `
        <div class="bar"><span class="meta">Depth ${i + 1}</span>
          <span class="bar__track" role="meter" aria-valuemin="0" aria-valuemax="${d.budget}" aria-valuenow="${d.spent}" aria-label="Depth ${i + 1} pages"><span class="bar__fill" style="width:${d.budget ? Math.min(100, (100 * d.spent) / d.budget) : 0}%"></span></span>
          <span class="meta">${d.spent}/${d.budget}</span></div>`).join("")
      : "";

    const err = p?.error || s?.error;
    main.querySelector("#alert").innerHTML = err ? `<p class="notice notice--error" style="margin-top:var(--space-4)">${esc(err)}</p>` : "";
    main.querySelector("#cancel").hidden = !live;
    for (const b of main.querySelectorAll("[data-mode]")) {
      b.setAttribute("aria-pressed", String(b.dataset.mode === mode));
      if (b.dataset.mode === "report") b.disabled = !info.has_report && !live;
    }
  }

  // ---------- outline ----------
  function glyph(n) {
    if (n.kind === "root") return "TOPIC";
    if (n.kind === "query") return "?";
    if (n.kind === "concept") return n.ckind === "problem" ? "!" : "*";
    return n.sid ? `S${n.sid}` : `d${n.depth}`;
  }
  function metaLine(n) {
    const bits = [];
    if (n.kind === "source") {
      bits.push(esc(host(n.url)));
      if (n.rel != null) bits.push(`content ${fmt.p(n.rel)}`);
      if (n.paths) bits.push(`+${n.paths} paths`);
    } else if (n.kind === "query") {
      bits.push(esc(VIA[n.via] || n.via));
    } else if (n.kind === "concept") {
      bits.push(n.ckind === "problem" ? "problem" : "concept");
    }
    if (n.score != null) bits.push(`jev <span class="score" aria-hidden="true"><i style="width:${Math.round(n.score * 100)}%"></i></span> ${fmt.p(n.score)}`);
    if (n.status !== KEPT[n.kind] && n.kind !== "root") bits.push(esc(n.status.replace("_", " ")));
    return bits.join(" · ");
  }
  function nodeHTML(n, now) {
    const children = kids(n.id);
    const open = !collapsed.has(n.id);
    const pruned = n.kind !== "root" && ["skipped", "failed", "over_budget"].includes(n.status);
    const cls = ["node", `node--${n.kind}`, pruned ? "node--pruned" : "", n.status === "queued" || n.status === "pending" ? "node--queued" : "", now - (fresh.get(n.id) || 0) < 4000 ? "node--new" : ""].join(" ");
    const toggle = n.kind === "root" ? "" : children.length
      ? `<button class="node__toggle" type="button" data-toggle="${n.id}" aria-expanded="${open}" aria-label="${open ? "Fold" : "Open"} ${esc(n.label)}">${open ? "−" : "+"}</button>`
      : `<span class="node__toggle node__toggle--leaf" aria-hidden="true"></span>`;
    return `
      <li class="node--${n.kind === "root" ? "root" : n.kind}">
        <div class="${cls}" ${selected === n.id ? 'aria-current="true"' : ""}>
          ${toggle}
          <button class="node__body" type="button" data-select="${n.id}">
            <span class="node__glyph">${glyph(n)}</span>
            <span class="node__title">${esc(n.label)}</span>
            ${n.kind === "root" ? "" : `<span class="node__meta">${metaLine(n)}${children.length && !open ? ` · ${children.length} hidden` : ""}</span>`}
          </button>
        </div>
        ${children.length && open ? `<ul>${children.map((c) => nodeHTML(c, now)).join("")}</ul>` : ""}
      </li>`;
  }
  function drawTree(canvas) {
    const r = root();
    if (!r) {
      canvas.innerHTML = `<p class="body">Planting the root…</p>`;
      return;
    }
    const focused = document.activeElement?.dataset?.select;
    canvas.innerHTML = `<ul class="tree">${nodeHTML(r, Date.now())}</ul>
      ${kids(r.id).length ? "" : `<p class="caption muted" style="margin-top:var(--space-3)">Seed queries appear here once the drafter and Jev have run.</p>`}`;
    if (focused) canvas.querySelector(`[data-select="${focused}"]`)?.focus();
  }

  // ---------- map ----------
  function drawMap(canvas) {
    const r = root();
    if (!r) return drawTree(canvas);
    const pos = new Map();
    let row = 0;
    let maxD = 0;
    const lay = (n, d) => {
      maxD = Math.max(maxD, d);
      const ks = collapsed.has(n.id) ? [] : kids(n.id);
      if (!ks.length) pos.set(n.id, { d, y: row++ });
      else {
        ks.forEach((k) => lay(k, d + 1));
        pos.set(n.id, { d, y: (pos.get(ks[0].id).y + pos.get(ks[ks.length - 1].id).y) / 2 });
      }
    };
    lay(r, 0);
    const COL = 250, ROW = 28, PAD = 20;
    const X = (d) => PAD + d * COL;
    const Y = (y) => PAD + y * ROW + ROW / 2;
    const text = (n) => {
      const t = n.kind === "source" && n.sid ? `S${n.sid} ${n.label}` : n.label;
      return t.length > 30 ? t.slice(0, 29) + "…" : t;
    };
    const width = (n) => text(n).length * (n.kind === "root" ? 9 : n.kind === "query" ? 7 : 6.8);
    let edges = "", marks = "";
    for (const [id, p] of pos) {
      const n = nodes.get(id);
      const x = X(p.d), y = Y(p.y);
      const parent = nodes.get(n.parent);
      if (parent && pos.has(parent.id)) {
        const pp = pos.get(parent.id);
        const px = Math.min(X(pp.d) + 12 + width(parent) + 6, x - 20);
        edges += `<path class="edge ${n.via === "link" ? "edge--link" : ""}" d="M${px} ${Y(pp.y)} H${x - 20} V${y} H${x - 5}"/>`;
      }
      const sel = id === selected;
      marks += `<g data-select="${id}" tabindex="0" role="button" aria-label="${esc(n.label)}">
        ${sel ? `<rect class="sel" x="${x + 6}" y="${y - 11}" width="${width(n) + 12}" height="22"/>` : ""}
        <rect class="hit" x="${x - 6}" y="${y - 12}" width="${COL - 24}" height="24"/>
        <circle class="dot ${n.kind === "source" ? "dot--source" : ""}" cx="${x}" cy="${y}" r="${n.kind === "root" ? 5 : 3.5}"/>
        <text class="lbl lbl--${n.kind} ${sel ? "lbl--sel" : ""}" x="${x + 12}" y="${y + 4}">${esc(text(n))}</text></g>`;
    }
    const w = X(maxD) + COL + PAD, h = Math.max(row, 1) * ROW + PAD * 2;
    canvas.innerHTML = `<div class="map"><svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" role="img" aria-label="Map of the knowledge tree">${edges}${marks}</svg></div>
      <p class="meta muted" style="margin-top:var(--space-2)">Solid lines: search and concept branches. Dashed: links Jev chose to follow.</p>`;
  }

  // ---------- report + template ----------
  async function drawReport(canvas) {
    if (!info.has_report) {
      canvas.innerHTML = `<div class="paper"><p class="body">${progress?.status === "running" ? "The report is written after gathering ends. Watch the tree grow in the meantime." : "This run has no report."}</p></div>`;
      return;
    }
    if (reportHtml == null) {
      canvas.innerHTML = `<div class="paper"><p class="body">Loading report…</p></div>`;
      reportHtml = linkCitations(await markdown(await api.report(runId)));
    }
    if (mode !== "report") return;
    canvas.innerHTML = `<article class="paper report">${reportHtml}</article>`;
  }
  function linkCitations(html) {
    const bySid = new Map([...nodes.values()].filter((n) => n.sid).map((n) => [n.sid, n.id]));
    const box = document.createElement("div");
    box.innerHTML = html;
    const walker = document.createTreeWalker(box, NodeFilter.SHOW_TEXT);
    const hits = [];
    while (walker.nextNode()) if (/\[S\d+/.test(walker.currentNode.nodeValue)) hits.push(walker.currentNode);
    for (const t of hits) {
      const span = document.createElement("span");
      span.innerHTML = esc(t.nodeValue).replace(/\[(S\d+(?:\s*[,;]\s*S\d+)*)\]/g, (_, list) =>
        "[" + list.replace(/S(\d+)/g, (m, d) => (bySid.has(Number(d)) ? `<a href="#" class="cite" data-select="${bySid.get(Number(d))}">${m}</a>` : m)) + "]");
      t.replaceWith(...span.childNodes);
    }
    return box.innerHTML;
  }
  function drawContext(canvas) {
    const t = info.summary?.settings?.template;
    canvas.innerHTML = `
      <div class="spot-section"><span class="spot-label">Stage 0 Context</span>
        <div class="paper"><pre class="pre">${esc(info.context || "Not written yet.")}</pre></div></div>
      ${t ? `<div class="spot-section" style="margin-top:var(--space-4)"><span class="spot-label">Starting Template</span>
        <dl class="kv">${["intent", "deliverable", "tone", "filter"].map((k) => `<dt>${k}</dt><dd>${esc(t[k])}</dd>`).join("")}
        <dt>seed plan</dt><dd>${t.facets.map((f) => `<code>${esc(f)}</code>`).join("<br>")}</dd></dl></div>` : ""}`;
  }

  function drawCanvas() {
    const canvas = main.querySelector("#canvas");
    if (!canvas) return;
    if (mode === "map") drawMap(canvas);
    else if (mode === "report") drawReport(canvas);
    else if (mode === "context") drawContext(canvas);
    else drawTree(canvas);
  }

  // ---------- detail ----------
  async function drawDetail(force = false) {
    const box = main.querySelector("#detail");
    const n = nodes.get(selected) || root();
    if (!n) return;
    const sig = JSON.stringify(n);
    if (!force && sig === selectedSig) return;
    selectedSig = sig;
    const path = lineage(n);
    const kidsAll = [...nodes.values()].filter((c) => c.parent === n.id);
    const byStatus = {};
    for (const c of kidsAll) byStatus[`${c.kind} ${c.status.replace("_", " ")}`] = (byStatus[`${c.kind} ${c.status.replace("_", " ")}`] || 0) + 1;

    box.innerHTML = `
      <div class="detail">
        <p class="meta" style="margin:0 0 var(--space-2)">${esc(n.kind === "root" ? "Topic" : n.kind)} · ${esc(n.status.replace("_", " "))} · ${glyph(n)}</p>
        <h2 class="detail__title">${esc(n.label)}</h2>
        ${n.url ? `<p class="caption" style="margin-bottom:var(--space-3)"><a href="${esc(safeUrl(n.url))}" target="_blank" rel="noopener noreferrer">${esc(n.url)} &nearr;</a></p>` : ""}
        ${path.length ? `<section class="spot-section"><span class="spot-label">How It Got Here</span>
          <ol class="plain">${path.map((p) => `<li><a href="#" data-select="${p.id}"><span class="node__glyph">${glyph(p)}</span> ${esc(p.label)}</a></li>`).join("")}
          <li><span class="node__glyph">${glyph(n)}</span> ${esc(n.label)} <span class="meta muted">via ${esc(VIA[n.via] || n.via)}</span></li></ol></section>` : ""}
        <section class="spot-section"><span class="spot-label">Decision</span>
          <dl class="kv">
            ${n.score != null ? `<dt>${GATE[n.kind] || "Score"}</dt><dd>${fmt.p(n.score)}</dd>` : ""}
            ${n.rel != null ? `<dt>Content relevance</dt><dd>${fmt.p(n.rel)}</dd>` : ""}
            ${n.kind === "source" ? `<dt>Depth</dt><dd>${n.depth}</dd>` : ""}
            ${n.paths ? `<dt>Other paths</dt><dd>${n.paths} more nodes point here</dd>` : ""}
            ${n.error ? `<dt>Error</dt><dd>${esc(n.error)}</dd>` : ""}
            ${Object.keys(byStatus).length ? `<dt>Children</dt><dd>${Object.entries(byStatus).map(([k, v]) => `${v} ${esc(k)}`).join(", ")}</dd>` : ""}
          </dl>
          <div id="more"></div>
        </section>
      </div>`;

    if (n.kind === "root") return;
    try {
      const full = await api.node(runId, n.id);
      if (selected !== n.id && !(selected == null && n.kind === "root")) return;
      const d = full.data || {};
      const more = [];
      const kv = [];
      if (full.reason) kv.push(["Why", full.reason]);
      if (d.fetched_by) kv.push(["Fetched by", d.fetched_by]);
      if (d.provider) kv.push(["Found by", d.provider]);
      if (d.results != null) kv.push(["Results", d.results]);
      if (d.mentions) kv.push(["Mentions", `${d.mentions} pages`]);
      if (d.truncated) kv.push(["Text", "truncated to the page budget"]);
      if (d.stop) kv.push(["Delving", "stopped here"]);
      if (kv.length) more.push(`<dl class="kv" style="margin-top:var(--space-2)">${kv.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("")}</dl>`);
      if (Array.isArray(d.delve) && d.delve.length) {
        more.push(`<section class="spot-section"><span class="spot-label">Links Jev Followed</span><ol class="plain">${d.delve.map((l) => `<li>${fmt.p(l.confidence)} · <a href="${esc(safeUrl(l.url))}" target="_blank" rel="noopener noreferrer">${esc(l.why || l.url)}</a></li>`).join("")}</ol></section>`);
      }
      if (full.text) {
        const body = full.text.replace(/^# .*\n<[^>]*>\n\n/, "");
        more.push(`<section class="spot-section"><span class="spot-label">Page Text</span><div class="paper excerpt"><pre class="pre">${esc(body.slice(0, 6000))}${body.length > 6000 ? "\n…" : ""}</pre></div></section>`);
      }
      box.querySelector("#more").innerHTML = more.join("");
    } catch {
      /* the node may be too new for tree.json; the summary above stands */
    }
  }

  // ---------- wire ----------
  function wireLine(e) {
    const t = `<span class="muted">${String(e.t).padStart(5)}s</span> `;
    switch (e.kind) {
      case "seeds": return t + `${e.source.startsWith("llm") ? "Drafter" : "Needle"} wrote ${e.queries.length} seed queries (${esc(e.source)})`;
      case "gate": return t + `Jev kept ${e.ran.length}/${e.total} queries`;
      case "frontier": return t + `Frontier holds ${e.size} sources`;
      case "page": return t + `${e.via === "link" ? "↳" : "•"} d${e.depth} ${fmt.p(e.rel)} <a href="#" data-select="${e.id}">${esc(e.label.slice(0, 80))}</a> <span class="muted">${esc(host(e.url))}</span>`;
      case "expand": return t + `Concepts grew ${e.queries.length} branches: ${e.queries.map((q) => `<a href="#" data-select="${q.id}">${esc(q.label)}</a>`).join(", ") || "none"}`;
      default: return t + esc(e.kind);
    }
  }
  function drawWire() {
    const box = main.querySelector("#wire");
    if (!box) return;
    box.innerHTML = events.length ? events.slice(-120).reverse().map((e) => `<li>${wireLine(e)}</li>`).join("") : `<li class="muted">No events yet.</li>`;
  }

  // ---------- events ----------
  function select(id, scroll = true) {
    if (!nodes.has(id)) return;
    selected = id;
    for (let p = nodes.get(nodes.get(id).parent); p; p = nodes.get(p.parent)) collapsed.delete(p.id);
    if (!visible(nodes.get(id))) {
      showAll = true;
      index = null;
      main.querySelector("#show-all").checked = true;
    }
    if (mode !== "report") drawCanvas();
    drawDetail(true);
    if (scroll && window.matchMedia("(max-width:1024px)").matches) main.querySelector("#detail").scrollIntoView({ behavior: "smooth", block: "start" });
  }
  function onCanvas(e) {
    const t = e.target.closest("[data-toggle]");
    if (t) {
      const id = t.dataset.toggle;
      collapsed.has(id) ? collapsed.delete(id) : collapsed.add(id);
      drawCanvas();
      return;
    }
    const s = e.target.closest("[data-select]");
    if (s) {
      e.preventDefault();
      select(s.dataset.select);
    }
  }
  function onDetail(e) {
    const s = e.target.closest("[data-select]");
    if (s) {
      e.preventDefault();
      if (mode === "report" || mode === "context") setMode("tree");
      select(s.dataset.select, false);
      main.querySelector(`#canvas [data-select="${s.dataset.select}"]`)?.scrollIntoView({ block: "center" });
    }
  }
  function onKey(e) {
    const cur = e.target.closest("[data-select]")?.dataset.select;
    if (!cur || mode !== "tree") return;
    const order = [];
    const walk = (n) => { order.push(n.id); if (!collapsed.has(n.id)) kids(n.id).forEach(walk); };
    if (root()) walk(root());
    const i = order.indexOf(cur);
    let next = null;
    if (e.key === "ArrowDown") next = order[i + 1];
    else if (e.key === "ArrowUp") next = order[i - 1];
    else if (e.key === "ArrowLeft") { if (!collapsed.has(cur) && kids(cur).length) { collapsed.add(cur); drawCanvas(); } else next = nodes.get(cur).parent; }
    else if (e.key === "ArrowRight") { if (collapsed.has(cur)) { collapsed.delete(cur); drawCanvas(); } else next = kids(cur)[0]?.id; }
    else return;
    e.preventDefault();
    if (next) {
      select(next, false);
      main.querySelector(`#canvas [data-select="${next}"]`)?.focus();
    }
  }
  function setMode(m) {
    mode = m;
    sessionStorage.setItem("sr.runMode", m);
    drawHeader();
    drawCanvas();
  }
  async function onToolbar(e) {
    const b = e.target.closest("button");
    if (!b) return;
    if (b.dataset.mode) setMode(b.dataset.mode);
    else if (b.id === "fold") {
      for (const n of nodes.values()) if (n.kind === "source" && kids(n.id).length) collapsed.add(n.id);
      drawCanvas();
    } else if (b.id === "open") {
      collapsed.clear();
      drawCanvas();
    } else if (b.id === "cancel") {
      if (!confirm("Stop this run? The tree so far is kept. No report is written.")) return;
      try {
        await api.cancel(runId);
        toast("Stopping");
      } catch (err) {
        toast(err.message);
      }
    }
  }

  // ---------- live stream ----------
  function connect() {
    const after = events.length ? events[events.length - 1].seq : 0;
    es = api.stream(runId, after);
    es.onmessage = (m) => {
      const p = JSON.parse(m.data);
      progress = p.progress;
      merge(p.nodes, p.full);
      if (p.events.length) events = events.concat(p.events).slice(-400);
      if (selected == null && root()) selected = root().id;
      drawHeader();
      if (mode === "tree" || mode === "map") drawCanvas();
      drawDetail();
      if (p.events.length) drawWire();
    };
    es.addEventListener("end", () => {
      es.close();
      es = null;
      setTimeout(load, 600); // pick up run.json, S# ids and the report
    });
    es.onerror = () => {
      if (es && es.readyState === EventSource.CLOSED && !closed) setTimeout(load, 2000);
    };
  }

  async function load() {
    if (closed) return;
    try {
      info = await api.run(runId);
    } catch (e) {
      main.innerHTML = `<div class="hero-block"><h1 class="spot-hero">No run<br>here.</h1></div><p class="lead">${esc(e.message)}. <a href="#/runs">See all runs</a>.</p>`;
      return;
    }
    const first = !main.querySelector("#canvas");
    if (first) frame();
    progress = info.progress;
    events = info.events.length ? info.events : events;
    merge(info.nodes, true);
    reportHtml = null;
    if (selected == null || !nodes.has(selected)) selected = root()?.id ?? null;
    if (!info.live && mode === "tree" && info.has_report && first && !sessionStorage.getItem("sr.runMode")) mode = "report";
    drawHeader();
    drawCanvas();
    drawDetail(true);
    drawWire();
    if (info.live && !es) connect();
  }

  load();
  return () => {
    closed = true;
    if (es) es.close();
  };
}
