"""CLI entry and orchestration.

    uv run research "gradient surgery methods and results"

Stages: 0 context template -> 1 Needle drafts queries -> 2 Jev gates queries ->
3 SearXNG -> 4 Jev rates results -> 5 scrape -> 6 Jev picks links to delve (recurse)
-> concept expansion (Needle extracts, Jev gates, back to 3) -> 7 one LLM writes the report.
Everything lands in the knowledge tree; see tree_manager.py.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import datetime as dt
import json
import logging
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx

from . import config as cfg
from . import context, report, scraper, searx, tavily_client
from .needle_client import problem_phrases
from .jev_client import JEV_PRICE_PER_MTOK, JevBudgetExceeded, JevJudge, StubJudge
from .schemas import Node
from .tree_manager import KnowledgeTree, normalize_url

log = logging.getLogger("research")

SearchFn = Callable[[str], Awaitable[list[searx.SearchResult]]]
FetchFn = Callable[[str], Awaitable[scraper.Page]]


class JsonlLog:
    def __init__(self, path: Path):
        self.f = path.open("a")

    def __call__(self, rec: dict) -> None:
        self.f.write(json.dumps({"t": round(time.time(), 3), **rec}, default=str) + "\n")
        self.f.flush()


def plain_emit(kind: str, **kw) -> None:
    """Pipeline events as log lines (used with --plain or when not on a TTY)."""
    if kind == "seeds":
        log.info("needle drafted %d queries (%s)", len(kw["drafts"]), kw["source"])
    elif kind == "gate":
        log.info("jev kept %d/%d queries: %s", len(kw["ran"]), kw["total"], [n.label for n in kw["ran"]])
    elif kind == "frontier":
        log.info("frontier: %d queued sources", kw["size"])
    elif kind == "page":
        n = kw["node"]
        log.info("  d%d %.2f  %s", n.depth, n.data.get("page_relevant", 0), n.url)
    elif kind == "expand":
        log.info("concept expansion: %s", [q.label for q in kw["queries"]])


class Researcher:
    def __init__(
        self,
        ctx: context.ResearchContext,
        settings: cfg.Settings,
        run_dir: Path,
        judge,
        needle,
        search_fn: SearchFn,
        fetch_fn: FetchFn,
    ):
        self.ctx = ctx
        self.s = settings
        self.b = settings.budgets
        self.g = settings.gates
        self.dir = run_dir
        self.judge = judge
        self.needle = needle
        self.search_fn = search_fn
        self.fetch_fn = fetch_fn
        self.tree = KnowledgeTree(ctx.topic, self.b, self.g)
        self.research = ctx.as_state()
        self.deadline = time.monotonic() + self.b.max_seconds
        self.stop_reason: str | None = None
        self.timings: dict[str, float] = {}
        self.stage = "starting"  # read by the live UI
        self.emit: Callable[..., None] = plain_emit  # the rich UI swaps this out
        # Pages a search backend already fetched (Tavily raw content), keyed by node id.
        self.prefetched: dict[str, scraper.Page] = {}
        (run_dir / "pages").mkdir(exist_ok=True)

    # --- helpers --------------------------------------------------------------

    def out_of_time(self) -> bool:
        if time.monotonic() > self.deadline:
            self.stop_reason = self.stop_reason or f"time budget ({self.b.max_seconds}s) spent"
            return True
        return False

    def halted(self) -> bool:
        return self.stop_reason is not None or self.out_of_time()

    async def _guard(self, coro):
        """Budget exhaustion stops gathering but keeps what was collected."""
        try:
            return await coro
        except JevBudgetExceeded as e:
            self.stop_reason = self.stop_reason or str(e)
            return None

    # --- stage 1-2 --------------------------------------------------------------

    async def seed_queries(self) -> list[Node]:
        self.stage = "1 · Needle drafts queries"
        drafts, source = await self.needle.draft_queries(self.ctx.needle_prompts(self.b.seed_queries))
        self.emit("seeds", drafts=drafts, source=source)
        self.tree.root.data["seed_source"] = source
        nodes = [n for q in drafts[: self.b.seed_queries] if (n := self.tree.add_query(q, self.tree.root.id, "needle_seed"))]
        already = [n.label for n in self.tree.of("query", "ran")]
        self.stage = "2 · Jev gates queries"
        scores = await self._guard(self.judge.gate_queries(self.research, [n.label for n in nodes], already))
        if scores is None:
            return []
        for n, p in zip(nodes, scores):
            n.score = p
            n.status = "ran" if p >= self.g.query else "skipped"
        ran = [n for n in nodes if n.status == "ran"]
        self.emit("gate", ran=ran, total=len(nodes))
        return ran

    # --- stage 3-4 --------------------------------------------------------------

    async def search_and_rate(self, q: Node) -> None:
        try:
            results = await self.search_fn(q.label)
        except Exception as e:
            q.data["error"] = repr(e)
            log.warning("search failed for %r: %r", q.label, e)
            return
        results = [r for r in results if not self.tree.known_url(r.url)]
        q.data["results"] = len(results)
        if not results:
            return
        scores = await self._guard(self.judge.rate_results(self.research, q.label, results))
        if scores is None:
            return
        for r, p in zip(results, scores):
            node = self.tree.add_source(r.url, r.title, q.id, "search", depth=1, reason=r.snippet[:200], engines=r.engines, provider=r.provider)
            if node is None:
                continue
            node.score = p
            if p >= self.g.relevance and len(r.content) >= 500:
                self.prefetched[node.id] = scraper.page_from_markdown(node.url, r.title, r.content, self.b.page_tokens)
            if p >= self.g.relevance:
                self.tree.enqueue(node)
            else:
                node.status = "skipped"

    async def search_round(self, queries: list[Node]) -> None:
        self.stage = f"3-4 · searching {len(queries)} queries, Jev rates results"
        await asyncio.gather(*(self.search_and_rate(q) for q in queries))
        self.emit("frontier", size=self.tree.frontier_size())

    # --- stage 5-6 --------------------------------------------------------------

    async def process(self, node: Node) -> None:
        try:
            if page := self.prefetched.pop(node.id, None):
                node.data["fetched_by"] = "search backend"
            else:
                page = await self.fetch_fn(node.url)
                node.data["fetched_by"] = getattr(page, "fetched_by", "scraper")
        except Exception as e:
            node.status = "failed"
            node.data["error"] = str(e)[:200]
            self.tree.spent[node.depth - 1] -= 1  # a failed fetch doesn't consume budget
            return
        if dup := self.tree.resolve_redirect(node, page.url):
            node.status = "failed"
            node.data["error"] = f"redirected to already-known {dup.id}"
            self.tree.spent[node.depth - 1] -= 1
            return
        node.status = "scraped"
        node.label = page.title or node.label
        node.data.update(text=page.text, truncated=page.truncated, n_anchors=len(page.anchors))
        (self.dir / "pages" / f"{node.id}.md").write_text(f"# {page.title}\n<{node.url}>\n\n{page.text}")

        rel = await self._guard(self.judge.page_relevance(self.research, page))
        if rel is None:
            return
        node.data["page_relevant"] = rel
        self.emit("page", node=node)

        tasks = []
        if rel >= 0.5:
            tasks.append(self.extract_concepts(node, page))
        # Stage 6. Delving only from pages whose own content held up (stop otherwise).
        can_delve = rel >= self.g.page_for_delve and node.depth < self.b.max_depth and self.tree.has_room(node.depth + 1)
        node.data["stop"] = not can_delve
        if can_delve:
            tasks.append(self.delve(node, page))
        await asyncio.gather(*tasks)

    async def delve(self, node: Node, page: scraper.Page) -> None:
        anchors = [a for a in page.anchors if not self.tree.known_url(a.url)][: self.b.anchors_per_page]
        scores = await self._guard(self.judge.rate_links(self.research, page, anchors))
        if not scores:
            return
        ranked = sorted(zip(anchors, scores), key=lambda t: -t[1])
        picks = [(a, p) for a, p in ranked if p >= self.g.delve][: self.b.delve_per_page]
        node.data["delve"] = [{"url": a.url, "why": a.text, "confidence": p} for a, p in picks]
        for a, p in picks:
            child = self.tree.add_source(a.url, a.text, node.id, "link", depth=node.depth + 1, reason=a.text)
            if child:
                child.score = p
                self.tree.enqueue(child)

    async def extract_concepts(self, node: Node, page: scraper.Page) -> None:
        terms = await self.needle.extract_terms(page.title, page.text, self.ctx.core)
        for t in terms:
            self.tree.add_concept(t, node.id)
        for p in problem_phrases(page.text, self.ctx.core):
            self.tree.add_concept(p, node.id, kind="problem")

    async def crawl(self, reserve: bool = False) -> None:
        self.stage = "5-6 · scraping, Jev picks links to delve"
        cap = None
        if reserve and self.b.expansion_rounds > 0:
            cap = round(self.b.pages_per_depth[0] * (1 - self.b.expansion_reserve))
        while not self.halted() and not self.tree.exhausted():
            batch = self.tree.pop_batch(self.s.concurrency, depth1_cap=cap)
            if not batch:
                break
            await asyncio.gather(*(self.process(n) for n in batch))

    # --- concept expansion --------------------------------------------------------

    async def expand(self) -> bool:
        """Turn concepts found on pages into new search branches: named methods/datasets
        and technical problems, each through its own Jev gate and query budget. Returns
        whether any new queries ran."""
        if self.tree.depth1_room() <= 0:
            return False
        self.stage = "↻ · concept expansion"
        kinds = [
            ("name", self.judge.gate_concepts, self.g.concept, self.b.expansion_queries, "needle_concept"),
            ("problem", self.judge.gate_problems, self.g.problem, self.b.expansion_problem_queries, "problem_concept"),
        ]
        batches = await asyncio.gather(*(self._promote(*k) for k in kinds))
        queries = [q for batch in batches for q in batch]
        self.emit("expand", queries=queries)
        if not queries:
            return False
        await self.search_round(queries)
        return True

    async def _promote(self, kind: str, gate_fn, gate: float, budget: int, via: str) -> list[Node]:
        cands = self.tree.concept_candidates(kind)[: budget * 4]
        if not cands or budget <= 0:
            return []
        scores = await self._guard(gate_fn(self.research, [(t, n) for t, n, _ in cands]))
        if scores is None:
            return []
        queries: list[Node] = []
        for (term, mentions, ids), p in sorted(zip(cands, scores), key=lambda t: -t[1]):
            for cid in ids:
                self.tree.nodes[cid].score = p
            if p < gate or len(queries) >= budget:
                for cid in ids:
                    self.tree.nodes[cid].status = "skipped"
                continue
            # The query hangs off the first page that mentioned the concept; other
            # mentions are recorded as also_from.
            anchor = self.tree.nodes[ids[0]]
            anchor.status = "promoted"
            for cid in ids[1:]:
                self.tree.nodes[cid].status = "promoted"
                anchor.also_from.append(self.tree.nodes[cid].parent)
            q = self.tree.add_query(f"{term} {self.ctx.core}", anchor.id, via)
            if q:
                q.score, q.status = p, "ran"
                q.data["mentions"] = mentions
                queries.append(q)
        return queries

    # --- whole pass ----------------------------------------------------------------

    async def gather(self) -> None:
        t0 = time.monotonic()
        seeds = await self.seed_queries()
        await self.search_round(seeds)
        await self.crawl(reserve=True)
        for _ in range(self.b.expansion_rounds):
            if self.halted() or not await self.expand():
                break
            await self.crawl()
        await self.crawl()  # spend any leftover budget on the remaining frontier
        self.timings["gather_s"] = round(time.monotonic() - t0, 1)
        if not self.stop_reason:
            self.stop_reason = "page budget spent" if self.tree.exhausted() else "frontier empty"


# --- wiring ----------------------------------------------------------------------


def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:60] or "research"


def backends(settings: cfg.Settings) -> list[str]:
    names = list(settings.search_backends)
    if "auto" in names:
        names = ["tavily", "searxng"] if tavily_client.available() else ["searxng"]
    if "tavily" in names and not tavily_client.available():
        log.warning("TAVILY_API_KEY not set; dropping the tavily backend")
        names.remove("tavily")
    return names


async def run(
    topic: str,
    settings: cfg.Settings,
    intent: str | None,
    focus: list[str],
    write_report: bool = True,
    ui=None,
) -> Path:
    ctx = context.build(topic, intent, focus)
    run_dir = settings.reports_dir / slugify(topic) / dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    plan = "\n".join(p for p, _ in ctx.needle_prompts(settings.budgets.seed_queries))
    (run_dir / "context_prompt.txt").write_text(f"{ctx.render()}\n\n--- needle input ---\n{plan}\n")
    run_id = uuid.uuid4().hex
    t_start = time.monotonic()

    jev_log = JsonlLog(run_dir / "jev_verdicts.jsonl")
    needle_log = JsonlLog(run_dir / "needle_drafts.jsonl")
    if settings.offline:
        from .needle_client import StubNeedle

        judge, needle = StubJudge(jev_log), StubNeedle()
    else:
        from .needle_client import NeedleClient

        judge = JevJudge(settings.jev_model, settings.budgets.max_jev_calls, jev_log, settings.concurrency)
        needle = NeedleClient(needle_log)

    for noisy in ("httpx", "httpx2", "httpcore", "typesafe_sdk"):  # needle's import resets these
        logging.getLogger(noisy).setLevel(logging.WARNING)
    http = scraper.new_client()
    # SearXNG's limiter stalls requests with a browser UA but no browser headers, so
    # search gets its own plainly-identified client.
    searx_http = httpx.AsyncClient(headers={"User-Agent": "super-research/0.1"})
    searx_sem = asyncio.Semaphore(settings.searx_concurrency)

    use = backends(settings)
    search_log = JsonlLog(run_dir / "search.jsonl")
    tavily = None
    if "tavily" in use or (settings.tavily_extract_fallback and tavily_client.available()):
        tavily = tavily_client.TavilySearch(
            settings.tavily_depth,
            settings.tavily_max_results,
            settings.tavily_prefer_domains,
            settings.budgets.page_tokens,
            search_log,
        )

    async def searxng(q: str):
        async with searx_sem:
            results, dead = await searx.search(searx_http, settings.searx_url, q, settings.searx_engines, settings.budgets.results_per_query)
        search_log({"kind": "searxng", "query": q, "n": len(results), "unresponsive": dead})
        return results

    providers = {"tavily": tavily.search if tavily else None, "searxng": searxng}

    async def search_fn(q: str):
        """All backends in parallel; merged by URL, earlier backends win duplicates.
        One backend failing (rate limit, captcha) doesn't fail the query."""
        got = await asyncio.gather(*(providers[b](q) for b in use), return_exceptions=True)
        merged: dict[str, searx.SearchResult] = {}
        errors = []
        for name, res in zip(use, got):
            if isinstance(res, BaseException):
                errors.append(f"{name}: {res!r}")
                continue
            for item in res:
                merged.setdefault(normalize_url(item.url), item)
        if errors and not merged:
            raise RuntimeError("; ".join(errors))
        for e in errors:
            log.warning("search backend failed for %r: %s", q, e)
        return list(merged.values())

    async def fetch_fn(url: str):
        # arXiv: the /html/ version is the full paper; /abs/ is only the abstract.
        if m := re.match(r"https?://arxiv\.org/abs/(.+)$", url):
            try:
                page = await scraper.fetch(http, f"https://arxiv.org/html/{m.group(1)}", settings.budgets.page_tokens)
                page.fetched_by = "scraper (arxiv html)"
                return page
            except scraper.ScrapeError:
                pass
        try:
            return await scraper.fetch(http, url, settings.budgets.page_tokens)
        except scraper.ScrapeError:
            if not (tavily and settings.tavily_extract_fallback):
                raise
            page = await tavily.extract(url)
            page.fetched_by = "tavily extract"
            return page

    log.info("search backends: %s", ", ".join(use))
    r = Researcher(ctx, settings, run_dir, judge, needle, search_fn, fetch_fn)
    if ui:
        ui.attach(r, judge, tavily)
    result = None
    error = None
    try:
        await r.gather()
        log.info("gathering stopped: %s | %s", r.stop_reason, r.tree.stats()["pages_per_depth_spent"])
        r.tree.save(run_dir / "tree.json")  # inspectable while the report is being written
        (run_dir / "tree.md").write_text(r.tree.render(include_pruned=True, max_children=200))
        if write_report and not r.tree.scraped_sources():
            raise RuntimeError("no pages were scraped; not calling the report LLM (see run.json / tree.md)")
        if write_report:
            t = time.monotonic()
            if settings.offline:
                result = report.offline_report(ctx, r.tree, settings.budgets.report_context_tokens)
            else:
                r.stage = f"7 · {settings.report_model} writes the report"
                log.info("writing report with %s ...", settings.report_model)
                result = await report.write(
                    ctx,
                    r.tree,
                    base_url=settings.opencode_url,
                    model=settings.report_model,
                    context_tokens=settings.budgets.report_context_tokens,
                    max_output_tokens=settings.budgets.report_max_output_tokens,
                    session_id=run_id,
                    prompt_path=run_dir / "report_prompt.txt",
                )
            r.timings["report_s"] = round(time.monotonic() - t, 1)
            (run_dir / "report.md").write_text(result.markdown)
    except Exception as e:
        error = repr(e)
        raise
    finally:
        if ui:
            ui.close()
        await http.aclose()
        await searx_http.aclose()
        if isinstance(judge, JevJudge):
            await judge.close()
        r.tree.save(run_dir / "tree.json")
        (run_dir / "tree.md").write_text(r.tree.render(include_pruned=True, max_children=200))
        r.stage = "done"
        jev_cost = judge.input_tokens * JEV_PRICE_PER_MTOK / 1e6
        tavily_cost = tavily.usd if tavily else 0.0
        llm_cost = result.cost_usd if result else 0.0
        summary: dict[str, Any] = {
            "topic": topic,
            "run_id": run_id,
            "stop_reason": r.stop_reason,
            "error": error,
            "timings": {**r.timings, "total_s": round(time.monotonic() - t_start, 1)},
            "tree": r.tree.stats(),
            "jev": {"calls": judge.calls, "input_tokens": judge.input_tokens, "cost_usd": round(jev_cost, 5)},
            "needle": {"calls": getattr(needle, "calls", 0), "seed_source": r.tree.root.data.get("seed_source")},
            "search": {
                "backends": use,
                "tavily_credits": tavily.credits if tavily else 0,
                "tavily_searches": tavily.searches if tavily else 0,
                "tavily_extracts": tavily.extracts if tavily else 0,
                "tavily_usd": round(tavily_cost, 4),
            },
            "report": dataclasses.asdict(result) | {"markdown": None} if result else None,
            "cost_usd_total": round(jev_cost + tavily_cost + (llm_cost or 0.0), 4) if llm_cost is not None else None,
            "settings": json.loads(json.dumps(dataclasses.asdict(settings), default=str)),
        }
        (run_dir / "run.json").write_text(json.dumps(summary, indent=2, default=str))
        for f in (jev_log, needle_log, search_log):
            f.f.close()
    return run_dir


def _parse_value(v: str) -> Any:
    try:
        return json.loads(v)
    except json.JSONDecodeError:
        if "," in v:
            return [_parse_value(x) for x in v.split(",")]
        return v


def build_overrides(a: argparse.Namespace) -> dict[str, Any]:
    o: dict[str, Any] = {"budgets": {}, "gates": {}}
    if a.pages:
        o["budgets"]["pages_per_depth"] = [int(x) for x in a.pages.split(",")]
    for flag, key in [
        ("seed_queries", "seed_queries"),
        ("expansion_rounds", "expansion_rounds"),
        ("max_seconds", "max_seconds"),
        ("max_jev_calls", "max_jev_calls"),
        ("report_tokens", "report_context_tokens"),
        ("page_tokens", "page_tokens"),
    ]:
        if (v := getattr(a, flag)) is not None:
            o["budgets"][key] = v
    for g in a.gate or []:
        k, _, v = g.partition("=")
        o["gates"][k] = float(v)
    if a.model:
        o["report_model"] = a.model
    if a.searx:
        o["searx_url"] = a.searx
    if a.engines:
        o["searx_engines"] = [e.strip() for e in a.engines.split(",")]
    if a.offline:
        o["offline"] = True
    for kv in a.set or []:
        k, _, v = kv.partition("=")
        section, _, name = k.rpartition(".")
        (o[section] if section else o)[name] = _parse_value(v)
    return {k: v for k, v in o.items() if v != {}}


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="research", description="Needle + Jev + SearXNG research agent; one LLM call writes the report.")
    p.add_argument("topic", nargs="?")
    p.add_argument("--intent", help="override the templated intent sentence")
    p.add_argument("--focus", action="append", default=[], help="term to cover explicitly (repeatable), e.g. --focus PCGrad")
    b = p.add_argument_group("budgets (see config.py; also settable in research.toml)")
    b.add_argument("--preset", default="standard", choices=sorted(cfg.PRESETS))
    b.add_argument("--config", type=Path, help="TOML file with [budgets]/[gates] tables")
    b.add_argument("--pages", help="pages per depth, e.g. 30,15,8 (length = max depth)")
    b.add_argument("--seed-queries", type=int)
    b.add_argument("--expansion-rounds", type=int)
    b.add_argument("--max-seconds", type=int)
    b.add_argument("--max-jev-calls", type=int)
    b.add_argument("--report-tokens", type=int, help="page-context tokens for the final LLM")
    b.add_argument("--page-tokens", type=int, help="text tokens kept per page")
    b.add_argument("--gate", action="append", help="gate override NAME=P, e.g. --gate delve=0.6 (query, relevance, delve, concept, page_for_delve, depth_decay)")
    b.add_argument("--set", action="append", help="any setting, e.g. --set budgets.delve_per_page=3 or --set concurrency=4")
    p.add_argument("--model", help="OpenCode Go model for the report (default kimi-k3)")
    p.add_argument("--searx", help="SearXNG base URL")
    p.add_argument("--engines", help="comma-separated SearXNG engines")
    p.add_argument("--offline", action="store_true", help="stub Jev/Needle and skip the LLM (still searches and scrapes)")
    p.add_argument("--no-report", action="store_true", help="gather only; skip the final LLM call")
    p.add_argument("--print-config", action="store_true")
    p.add_argument("--plain", action="store_true", help="log lines instead of the rich live dashboard")
    p.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args(argv)

    fancy = not a.plain and sys.stderr.isatty() and not a.print_config
    if fancy:
        from . import ui

        ui.setup_logging(a.verbose)
    else:
        logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S", stream=sys.stderr)
    for noisy in ("httpx", "httpx2", "httpcore", "typesafe_sdk"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    settings = cfg.load(a.preset, a.config, build_overrides(a))
    if a.print_config:
        print(json.dumps(dataclasses.asdict(settings), indent=2, default=str))
        return
    if not a.topic:
        p.error("topic is required")
    if fancy:
        live = ui.LiveUI(a.topic, settings)
        try:
            run_dir = asyncio.run(run(a.topic, settings, a.intent, a.focus, write_report=not a.no_report, ui=live))
        finally:
            live.close()
        ui.summary(run_dir, json.loads((run_dir / "run.json").read_text()), live.r.tree if live.r else None)
        return
    run_dir = asyncio.run(run(a.topic, settings, a.intent, a.focus, write_report=not a.no_report))
    summary = json.loads((run_dir / "run.json").read_text())
    print(f"\nreport: {run_dir / 'report.md'}")
    print(f"tree:   {run_dir / 'tree.md'}")
    print(f"cost:   ${summary['cost_usd_total']}  time: {summary['timings']['total_s']}s  stop: {summary['stop_reason']}")


if __name__ == "__main__":
    main()
