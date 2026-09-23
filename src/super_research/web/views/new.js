import { api } from "../api.js";
import { getKeys, listSavedTemplates, saveTemplate, deleteTemplate, draft } from "../store.js";
import { esc, toast, debounce, fmt } from "../util.js";

// Starting templates that ship with the page, next to the server's default. Same fields
// and placeholders as config.Template.
const BUILTIN = [
  {
    id: "engineering",
    name: "Engineering Practice",
    template: {
      intent: "find engineering writeups, production case studies, benchmarks and code covering {topic}{focus}, plus any {year} updates.",
      deliverable: "a comparison of approaches in practice: trade-offs, failure modes, costs, plus what is missing.",
      tone: "practitioner oriented",
      filter: "skip marketing pages, skip beginner tutorials, skip SEO listicles.",
      facets: ["{core}", "{core} in production", "{core} case study", "{core} benchmark", "{core} github", "{core} postmortem", "{core} best practices {year}", "{core} limitations"],
    },
  },
  {
    id: "gap-hunt",
    name: "Gap Hunt",
    template: {
      intent: "find open problems, negative results and unresolved debates in {topic}{focus}, plus any {year} work.",
      deliverable: "a map of open problems, what has been tried, and where the evidence is thin.",
      tone: "research oriented, skeptical",
      filter: "skip product pages, skip tutorials below graduate level, skip SEO listicles.",
      facets: ["{core} open problems", "{core} limitations", "{core} negative results", "{core} failure modes", "{core} challenges survey", "{core} future work", "{core} reproducibility", "{core} {year}"],
    },
  },
];
const FIELDS = [
  ["intent", "Intent", "What the pass is for. Jev scores every query, result, page and link against it."],
  ["deliverable", "Deliverable", "What the report should give you."],
  ["tone", "Tone", ""],
  ["filter", "Filter", "What Jev skips."],
];

export function renderNew(main) {
  const saved = draft.get() || {};
  const state = {
    topic: saved.topic || "",
    focus: saved.focus || [],
    intent: saved.intent || "",
    templateId: saved.templateId || "default",
    template: saved.template || null,
    seed: saved.seed || "drafter",
    preset: saved.preset || "standard",
    basePreset: saved.basePreset || saved.preset || "standard",
    budgets: saved.budgets || null,
    gates: saved.gates || null,
    models: saved.models || null,
    writeReport: saved.writeReport ?? true,
  };
  let meta = null;

  main.innerHTML = `
    <div class="hero-block">
      <h1 class="spot-hero">Grow a<br>knowledge<br>tree.</h1>
    </div>
    <form class="stack" id="run-form" autocomplete="off">
      <section class="spot-section">
        <span class="spot-label">Topic</span>
        <label class="sr-only" for="topic">Research topic</label>
        <input class="topic-input" id="topic" placeholder="gradient surgery methods and results" maxlength="300" required value="${esc(state.topic)}">
        <div class="grid-2" style="margin-top:var(--space-4)">
          <div class="field">
            <label class="label" for="focus-in">Focus terms</label>
            <div class="row"><input class="input" id="focus-in" placeholder="PCGrad" style="flex:1;min-width:160px"><button class="spot-button" type="button" id="focus-add">+ Add</button></div>
            <div class="chips" id="focus"></div>
          </div>
          <div class="field">
            <label class="label" for="intent">Intent override</label>
            <input class="input" id="intent" placeholder="Leave empty to use the template intent" value="${esc(state.intent)}">
          </div>
        </div>
      </section>

      <section class="spot-section">
        <span class="spot-label">Starting Template</span>
        <p class="lead">The template turns your topic into the research context and the seed plan. It is the root of the tree and the brief every model reads.</p>
        <div class="row" id="tpl-tabs" role="group" aria-label="Templates"></div>
        <div class="grid-2" style="margin-top:var(--space-4)">
          <div id="tpl-edit"></div>
          <div>
            <span class="label" style="display:block;margin-bottom:var(--space-3)">Seed Tree Preview</span>
            <div class="spot-frame"><div class="spot-frame__in"></div><div id="preview"><p class="caption">Loading…</p></div></div>
            <details style="margin-top:var(--space-3)">
              <summary class="label">Context block every model reads</summary>
              <div class="paper" style="margin-top:var(--space-3)"><pre class="pre" id="ctx-block"></pre></div>
            </details>
          </div>
        </div>
      </section>

      <section class="spot-section">
        <span class="spot-label">Budget</span>
        <div class="row" style="margin-bottom:var(--space-4)">
          <div class="seg" role="group" aria-label="Preset" id="presets"></div>
          <span class="meta muted" id="preset-hint"></span>
        </div>
        <div class="grid-4" id="budget-fields"></div>
        <details>
          <summary class="label">Jev gates</summary>
          <p class="caption" style="margin:var(--space-3) 0">Probabilities from 0 to 1. Lower one toward 0.5 when the frontier collapses.</p>
          <div class="grid-4" id="gate-fields"></div>
        </details>
      </section>

      <section class="spot-section">
        <span class="spot-label">Models And Search</span>
        <div class="grid-4" id="model-fields"></div>
        <label class="check caption"><input type="checkbox" id="write-report" ${state.writeReport ? "checked" : ""}><span>Write the report. Off gathers the tree only, with no OpenCode spend.</span></label>
      </section>

      <section class="spot-section">
        <span class="spot-label">Start</span>
        <p class="body" id="key-state"></p>
        <div class="row">
          <button class="spot-button spot-button--accent" type="submit" id="start">Start Research &rarr;</button>
          <span class="meta" id="start-state" role="status"></span>
        </div>
      </section>
    </form>`;

  const $ = (s) => main.querySelector(s);

  // ---------- focus chips ----------
  function drawFocus() {
    $("#focus").innerHTML = state.focus.map((f, i) => `<span class="chip">${esc(f)}<button type="button" data-drop="${i}" aria-label="Remove ${esc(f)}">&times;</button></span>`).join("");
  }
  function addFocus() {
    const v = $("#focus-in").value.trim();
    if (v && !state.focus.includes(v) && state.focus.length < 12) state.focus.push(v);
    $("#focus-in").value = "";
    drawFocus();
    changed();
  }
  $("#focus-add").addEventListener("click", addFocus);
  $("#focus-in").addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); addFocus(); }
  });
  $("#focus").addEventListener("click", (e) => {
    const i = e.target.closest("[data-drop]")?.dataset.drop;
    if (i != null) { state.focus.splice(Number(i), 1); drawFocus(); changed(); }
  });

  // ---------- templates ----------
  function allTemplates() {
    return [{ id: "default", name: "Research Survey", template: meta.defaults.template, fixed: true }, ...BUILTIN.map((t) => ({ ...t, fixed: true })), ...listSavedTemplates()];
  }
  function currentEntry() {
    return allTemplates().find((t) => t.id === state.templateId) || allTemplates()[0];
  }
  function loadTemplate(id) {
    state.templateId = id;
    state.template = structuredClone(currentEntry().template);
    drawTemplates();
    changed();
  }
  function isEdited() {
    return JSON.stringify(state.template) !== JSON.stringify(currentEntry().template);
  }
  function drawTemplates() {
    const entry = currentEntry();
    $("#tpl-tabs").innerHTML = allTemplates()
      .map((t) => `<button type="button" class="spot-button" data-tpl="${esc(t.id)}" aria-pressed="${t.id === entry.id}" ${t.id === entry.id ? 'style="background:var(--ink);color:var(--on-ink);border-color:var(--ink)"' : ""}>${esc(t.name)}</button>`)
      .join("");
    const t = state.template;
    $("#tpl-edit").innerHTML = `
      ${FIELDS.map(([k, label, hint]) => `
        <div class="field">
          <label class="label" for="t-${k}">${label}</label>
          <textarea class="textarea" id="t-${k}" data-field="${k}" rows="${k === "tone" ? 1 : 2}">${esc(t[k])}</textarea>
          ${hint ? `<p class="caption muted">${hint}</p>` : ""}
        </div>`).join("")}
      <div class="field">
        <span class="label">Seed plan</span>
        <p class="caption muted">Placeholders: <code>{core}</code> <code>{topic}</code> <code>{year}</code> <code>{last_year}</code> <code>{focus}</code>. Focus terms add their own lines on top.</p>
        <ol class="facet-list">
          ${t.facets.map((f, i) => `
            <li><span class="meta muted idx">${i + 1}</span>
              <label class="sr-only" for="f-${i}">Seed ${i + 1}</label>
              <input class="input" id="f-${i}" data-facet="${i}" value="${esc(f)}">
              <button class="icon-btn" type="button" data-up="${i}" aria-label="Move up" ${i ? "" : "disabled"}>&uarr;</button>
              <button class="icon-btn" type="button" data-del="${i}" aria-label="Remove seed">&times;</button></li>`).join("")}
        </ol>
        <div class="row"><button class="spot-button" type="button" id="facet-add" ${t.facets.length >= 24 ? "disabled" : ""}>+ Add Seed</button></div>
      </div>
      <div class="field">
        <span class="label">Seed source</span>
        <div class="seg" role="group" aria-label="Seed source">
          <button type="button" class="spot-button" data-seed="drafter" aria-pressed="${state.seed === "drafter"}">Drafter Writes Seeds</button>
          <button type="button" class="spot-button" data-seed="template" aria-pressed="${state.seed === "template"}">Use This Plan</button>
        </div>
        <p class="caption muted" id="seed-hint"></p>
      </div>
      <div class="row">
        <button class="spot-button" type="button" id="tpl-save">${entry.fixed ? "Save As New" : "Save"}</button>
        ${isEdited() ? `<button class="spot-button" type="button" id="tpl-reset">Reset</button>` : ""}
        ${entry.fixed ? "" : `<button class="spot-button" type="button" id="tpl-delete">Delete</button>`}
        <button class="spot-button" type="button" id="tpl-export">Export</button>
        <label class="spot-button" for="tpl-import">Import</label>
        <input class="sr-only" type="file" id="tpl-import" accept=".json,application/json">
      </div>`;
    drawSeedHint();
  }
  function drawSeedHint() {
    const model = state.models?.draft_model || meta.defaults.draft_model;
    $("#seed-hint").textContent = state.seed === "drafter"
      ? `${model} writes ${state.budgets?.seed_queries ?? "the"} seed queries in the field's vocabulary. This plan is the fallback if the call fails.`
      : "Needle splits this plan into the seed queries. No drafter call.";
  }

  $("#tpl-tabs").addEventListener("click", (e) => {
    const id = e.target.closest("[data-tpl]")?.dataset.tpl;
    if (id) loadTemplate(id);
  });
  $("#tpl-edit").addEventListener("input", (e) => {
    const el = e.target;
    if (el.dataset.field) state.template[el.dataset.field] = el.value;
    else if (el.dataset.facet != null) state.template.facets[Number(el.dataset.facet)] = el.value;
    else return;
    changed();
  });
  $("#tpl-edit").addEventListener("change", (e) => {
    if (e.target.id === "tpl-import") importTemplate(e.target);
  });
  $("#tpl-edit").addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    const f = state.template.facets;
    if (b.dataset.seed) { state.seed = b.dataset.seed; drawTemplates(); changed(); return; }
    if (b.id === "facet-add") f.push("{core} ");
    else if (b.dataset.del != null) f.splice(Number(b.dataset.del), 1);
    else if (b.dataset.up != null) { const i = Number(b.dataset.up); [f[i - 1], f[i]] = [f[i], f[i - 1]]; }
    else if (b.id === "tpl-reset") { loadTemplate(state.templateId); return; }
    else if (b.id === "tpl-save") {
      const entry = currentEntry();
      const name = entry.fixed ? prompt("Template name", `${entry.name} (mine)`)?.trim() : entry.name;
      if (!name) return;
      const id = entry.fixed ? "u-" + Math.random().toString(36).slice(2, 9) : entry.id;
      saveTemplate({ id, name: name.slice(0, 40), template: structuredClone(state.template) });
      state.templateId = id;
      toast("Template saved in this browser");
    } else if (b.id === "tpl-delete") {
      if (!confirm(`Delete “${currentEntry().name}”?`)) return;
      deleteTemplate(state.templateId);
      loadTemplate("default");
      return;
    } else if (b.id === "tpl-export") {
      const blob = new Blob([JSON.stringify({ name: currentEntry().name, template: state.template }, null, 2)], { type: "application/json" });
      const a = Object.assign(document.createElement("a"), { href: URL.createObjectURL(blob), download: "template.json" });
      a.click();
      setTimeout(() => URL.revokeObjectURL(a.href), 1000);
      return;
    } else return;
    drawTemplates();
    changed();
    if (b.id === "facet-add") $(`#f-${f.length - 1}`)?.focus();
  });
  async function importTemplate(input) {
    try {
      const data = JSON.parse(await input.files[0].text());
      const t = data.template || data;
      const clean = {
        intent: String(t.intent || ""), deliverable: String(t.deliverable || ""), tone: String(t.tone || ""), filter: String(t.filter || ""),
        facets: (Array.isArray(t.facets) ? t.facets : []).map(String).slice(0, 24),
      };
      const id = "u-" + Math.random().toString(36).slice(2, 9);
      saveTemplate({ id, name: String(data.name || "Imported").slice(0, 40), template: clean });
      loadTemplate(id);
      toast("Template imported");
    } catch {
      toast("That file is not a template");
    }
  }

  // ---------- preview ----------
  const refreshPreview = debounce(async () => {
    try {
      const p = await api.preview({ topic: state.topic || "your topic", intent: state.intent, focus: state.focus, template: state.template });
      const seeds = p.facets.slice(0, Number(state.budgets?.seed_queries) || 10);
      const drafter = state.seed === "drafter";
      $("#preview").innerHTML = `
        <ul class="tree">
          <li class="node--root">
            <div class="node"><span class="node__body"><span class="node__glyph">TOPIC</span><span class="node__title">${esc(state.topic || "Your topic")}</span></span></div>
            <ul>
              ${seeds.map((q) => `<li class="node--query"><div class="node"><span class="node__body"><span class="node__glyph">?</span><span class="node__title ${drafter ? "muted" : ""}">${esc(q)}</span></span></div></li>`).join("")}
            </ul>
          </li>
        </ul>
        <p class="meta muted" style="margin-top:var(--space-3)">${drafter ? "Fallback seeds. The drafter replaces these at run time." : `${seeds.length} seed queries. Jev gates each one, then sources, links and concepts grow beneath them.`} Core: “${esc(p.core)}”.</p>`;
      $("#ctx-block").textContent = p.context;
    } catch (e) {
      $("#preview").innerHTML = `<p class="caption">${esc(e.message)}</p>`;
    }
  }, 250);

  // ---------- budget, gates, models ----------
  const BUDGET = [
    ["pages_per_depth", "Pages per depth", "30,15,8. Length is the max depth."],
    ["seed_queries", "Seed queries", ""],
    ["expansion_rounds", "Concept rounds", "Times concepts grow new branches."],
    ["max_seconds", "Time cap (s)", ""],
    ["expansion_queries", "Concept queries", "Per round."],
    ["delve_per_page", "Links per page", "Jev may follow this many."],
    ["max_jev_calls", "Jev call cap", ""],
    ["report_context_tokens", "Report context tokens", ""],
  ];
  const GATES = ["query", "relevance", "delve", "concept", "problem", "page_for_delve", "depth_decay"];

  function applyPreset(name) {
    state.preset = name;
    state.basePreset = name;
    const p = meta.presets[name];
    state.budgets = Object.fromEntries(BUDGET.map(([k]) => [k, Array.isArray(p.budgets[k]) ? p.budgets[k].join(",") : p.budgets[k]]));
    drawBudget();
    changed();
  }
  function drawBudget() {
    $("#presets").innerHTML = Object.keys(meta.presets).map((n) => `<button type="button" class="spot-button" data-preset="${n}" aria-pressed="${state.preset === n}">${n[0].toUpperCase() + n.slice(1)}</button>`).join("");
    $("#budget-fields").innerHTML = BUDGET.map(([k, label, hint]) => `
      <div class="field"><label class="label" for="b-${k}">${label}</label>
        <input class="input" id="b-${k}" data-budget="${k}" value="${esc(state.budgets[k])}" inputmode="${k === "pages_per_depth" ? "text" : "numeric"}">
        ${hint ? `<p class="caption muted">${hint}</p>` : ""}</div>`).join("");
    $("#gate-fields").innerHTML = GATES.map((k) => `
      <div class="field"><label class="label" for="g-${k}">${k.replaceAll("_", " ")}</label>
        <input class="input" id="g-${k}" data-gate="${k}" type="number" min="0" max="1" step="0.05" value="${esc(state.gates[k])}"></div>`).join("");
    const pages = String(state.budgets.pages_per_depth).split(",").map(Number).filter(Boolean);
    $("#preset-hint").textContent = `${pages.reduce((a, b) => a + b, 0)} pages over ${pages.length} depths · up to ${fmt.secs(Number(state.budgets.max_seconds))}`;
  }
  function drawModels() {
    const m = state.models;
    const opt = (list, cur) => list.map((v) => `<option value="${esc(v)}" ${v === cur ? "selected" : ""}>${esc(v)}${meta.prices[v] ? ` · $${meta.prices[v][0]}/$${meta.prices[v][1]}` : ""}</option>`).join("");
    const models = meta.models.includes(m.report_model) ? meta.models : [m.report_model, ...meta.models];
    $("#model-fields").innerHTML = `
      <div class="field"><label class="label" for="m-report">Report model</label><select class="select" id="m-report" data-model="report_model">${opt(models, m.report_model)}</select></div>
      <div class="field"><label class="label" for="m-draft">Drafter model</label><select class="select" id="m-draft" data-model="draft_model">${opt(meta.models.includes(m.draft_model) ? meta.models : [m.draft_model, ...meta.models], m.draft_model)}</select></div>
      <div class="field"><label class="label" for="m-search">Search</label><select class="select" id="m-search" data-model="search_backends">
        ${[["auto", "Auto: Tavily + SearXNG"], ["tavily,searxng", "Tavily + SearXNG"], ["searxng", "SearXNG only (free)"], ["tavily", "Tavily only"]].map(([v, l]) => `<option value="${v}" ${m.search_backends === v ? "selected" : ""}>${l}</option>`).join("")}</select></div>
      <div class="field"><label class="label" for="m-depth">Tavily depth</label><select class="select" id="m-depth" data-model="tavily_depth">
        ${[["advanced", "Advanced · 2 credits"], ["basic", "Basic · 1 credit"]].map(([v, l]) => `<option value="${v}" ${m.tavily_depth === v ? "selected" : ""}>${l}</option>`).join("")}</select></div>`;
  }
  $("#presets").addEventListener("click", (e) => {
    const n = e.target.closest("[data-preset]")?.dataset.preset;
    if (n) applyPreset(n);
  });
  main.addEventListener("input", (e) => {
    const el = e.target;
    if (el.dataset.budget) { state.budgets[el.dataset.budget] = el.value; state.preset = "custom"; }
    else if (el.dataset.gate) state.gates[el.dataset.gate] = el.value;
    else if (el.dataset.model) { state.models[el.dataset.model] = el.value; drawSeedHint(); }
    else if (el.id === "topic") state.topic = el.value;
    else if (el.id === "intent") state.intent = el.value;
    else if (el.id === "write-report") state.writeReport = el.checked;
    else return;
    if (el.dataset.budget) {
      for (const b of main.querySelectorAll("[data-preset]")) b.setAttribute("aria-pressed", "false");
      if (el.dataset.budget === "seed_queries") drawSeedHint();
    }
    changed();
  });
  main.addEventListener("change", (e) => {
    if (e.target.id === "write-report") { state.writeReport = e.target.checked; changed(); }
  });

  // ---------- keys + start ----------
  function drawKeyState() {
    const k = getKeys();
    const srv = meta.server_keys;
    const has = (id) => k[id] || srv[id];
    const missing = [!has("typesafe") && "TypeSafe", !has("opencode") && (state.writeReport || state.seed === "drafter") && "OpenCode Go"].filter(Boolean);
    $("#key-state").innerHTML = missing.length
      ? `Missing ${missing.join(" and ")} key. <a href="#/keys" id="to-keys">Add your keys</a> before you start.`
      : `Keys ready${has("tavily") ? "" : ". No Tavily key: the run searches SearXNG only"}. They travel with this run only.`;
  }
  main.addEventListener("click", (e) => {
    if (e.target.id === "to-keys") sessionStorage.setItem("sr.returnTo", "#/");
  });

  function patch() {
    const b = state.budgets;
    const budgets = {};
    for (const [k] of BUDGET) {
      const v = b[k];
      if (k === "pages_per_depth") budgets[k] = String(v).split(",").map((x) => Number.parseInt(x, 10)).filter((x) => Number.isFinite(x));
      else if (v !== "" && Number.isFinite(Number(v))) budgets[k] = Number(v);
    }
    const gates = Object.fromEntries(Object.entries(state.gates).filter(([, v]) => v !== "" && Number.isFinite(Number(v))).map(([k, v]) => [k, Number(v)]));
    const m = state.models;
    return {
      budgets,
      gates,
      template: { ...state.template, facets: state.template.facets.map((f) => f.trim()).filter(Boolean) },
      report_model: m.report_model,
      draft_model: state.seed === "drafter" ? m.draft_model : "",
      tavily_depth: m.tavily_depth,
      search_backends: m.search_backends.split(","),
    };
  }

  $("#run-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!state.topic.trim()) return $("#topic").focus();
    const btn = $("#start");
    btn.disabled = true;
    $("#start-state").textContent = "Starting…";
    try {
      const { id } = await api.start({
        topic: state.topic,
        intent: state.intent,
        focus: state.focus,
        preset: state.basePreset || "standard",
        settings: patch(),
        keys: getKeys(),
        write_report: state.writeReport,
      });
      location.hash = `#/run/${encodeURIComponent(id)}`;
    } catch (err) {
      $("#start-state").textContent = err.message;
      btn.disabled = false;
    }
  });

  function changed() {
    draft.set(state);
    refreshPreview();
    if (meta) drawKeyState();
  }

  api.meta().then((m) => {
    meta = m;
    if (!state.template) state.template = structuredClone(currentEntry().template);
    state.gates ||= { ...m.defaults.gates };
    state.models ||= { report_model: m.defaults.report_model, draft_model: m.defaults.draft_model || "glm-5.3-flash", tavily_depth: m.defaults.tavily_depth, search_backends: m.defaults.search_backends.join(",") };
    if (!m.defaults.draft_model && !saved.seed) state.seed = "template";
    if (state.budgets) drawBudget(); else applyPreset(state.preset in m.presets ? state.preset : "standard");
    drawTemplates();
    drawModels();
    drawFocus();
    changed();
  }).catch((e) => {
    $("#preview").innerHTML = `<p class="notice notice--error">Cannot reach the server: ${esc(e.message)}</p>`;
  });
}
