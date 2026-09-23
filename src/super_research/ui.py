"""Rich terminal UI: a live dashboard while gathering and a summary at the end.
Purely cosmetic; --plain (or a non-TTY stderr) falls back to log lines."""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from urllib.parse import urlsplit

from rich import box
from rich.console import Console, Group
from rich.logging import RichHandler
from rich.markup import escape
from rich.panel import Panel
from rich.progress_bar import ProgressBar
from rich.table import Table
from rich.text import Text
from rich.tree import Tree

from .jev_client import JEV_PRICE_PER_MTOK

console = Console(stderr=True, highlight=False)


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(message)s",
        datefmt="%H:%M:%S",
        handlers=[RichHandler(console=console, show_path=False, markup=False, rich_tracebacks=True)],
    )


def _score_style(p: float | None) -> str:
    if p is None:
        return "dim"
    return "bold green" if p >= 0.8 else "green" if p >= 0.5 else "yellow" if p >= 0.3 else "red"


def _host(url: str | None) -> str:
    return urlsplit(url or "").netloc.removeprefix("www.")


class LiveUI:
    def __init__(self, topic: str, settings):
        self.topic = topic
        self.s = settings
        self.r = None
        self.judge = None
        self.tavily = None
        self.t0 = time.monotonic()
        self._task: asyncio.Task | None = None
        self._live = None

    # --- wiring -----------------------------------------------------------------

    def attach(self, researcher, judge, tavily) -> None:
        self.r, self.judge, self.tavily = researcher, judge, tavily
        researcher.emit = self.event
        from rich.live import Live

        # Manual refresh from the event loop: the tree is only mutated on this thread,
        # so rendering here never races the pipeline.
        self._live = Live(self.render(), console=console, auto_refresh=False, transient=True)
        self._live.start()
        self._task = asyncio.get_running_loop().create_task(self._tick())

    async def _tick(self) -> None:
        while True:
            self._live.update(self.render(), refresh=True)
            await asyncio.sleep(0.25)

    def close(self) -> None:
        """Idempotent: run() closes it before the loop ends, main() again on errors."""
        if self._task:
            self._task.cancel()
            self._task = None
        if self._live:
            self._live.stop()
            self._live = None

    # --- events (printed above the live panel) ------------------------------------

    def event(self, kind: str, **kw) -> None:
        p = console.print
        if kind == "seeds":
            p(f"[cyan]◆ needle[/] drafted {len(kw['drafts'])} queries [dim]({kw['source']})[/]")
        elif kind == "gate":
            ran = kw["ran"]
            p(f"[magenta]◆ jev[/] kept {len(ran)}/{kw['total']} queries")
            for q in ran:
                p(f"   [{_score_style(q.score)}]{q.score:.2f}[/]  {escape(q.label)}")
        elif kind == "frontier":
            p(f"[blue]◆ search[/] frontier now [bold]{kw['size']}[/] sources")
        elif kind == "page":
            n = kw["node"]
            rel = n.data.get("page_relevant")
            via = "↳" if n.via == "link" else "•"
            src = " [dim]tavily[/]" if n.data.get("fetched_by", "").startswith(("search", "tavily")) else ""
            p(
                f"   {via} [dim]d{n.depth}[/] [{_score_style(rel)}]{rel:.2f}[/] "
                f"{escape(n.label[:70])} [dim]{_host(n.url)}[/]{src}"
            )
        elif kind == "expand":
            names = ", ".join(("⚠ " if q.via == "problem_concept" else "") + escape(q.label) for q in kw["queries"]) or "none"
            p(f"[yellow]↻ concepts[/] → {names}")

    # --- live panel ---------------------------------------------------------------

    def render(self):
        elapsed = time.monotonic() - self.t0
        head = Text.assemble(
            ("● ", "bold green"),
            (self.r.stage if self.r else "starting", "bold"),
            ("   ", ""),
            (f"{elapsed:4.0f}s / {self.s.budgets.max_seconds}s", "dim"),
        )
        if not self.r:
            return Panel(head, title=f"[bold]{escape(self.topic)}[/]", border_style="cyan")

        tree = self.r.tree
        bars = Table.grid(padding=(0, 1))
        for d, (spent, cap) in enumerate(zip(tree.spent, self.s.budgets.pages_per_depth), 1):
            bars.add_row(f"[dim]depth {d}[/]", ProgressBar(total=cap, completed=min(spent, cap), width=28), f"{spent}/{cap}")

        stats = tree.stats()["by_kind_status"]
        c = lambda k: stats.get(k, 0)  # noqa: E731
        counts = Table.grid(padding=(0, 2))
        counts.add_row(
            f"queries [bold]{c('query:ran')}[/] ran [dim]{c('query:skipped')} skipped[/]",
            f"sources [bold]{c('source:scraped')}[/] read [dim]{c('source:queued')} queued · {c('source:skipped')} rejected · {c('source:failed')} failed[/]",
        )
        concepts = c("concept:pending") + c("concept:promoted") + c("concept:skipped")
        jev_usd = self.judge.input_tokens * JEV_PRICE_PER_MTOK / 1e6
        spend = f"jev [bold]{self.judge.calls}[/] calls ${jev_usd:.4f}"
        if self.tavily:
            spend += f"   tavily [bold]{self.tavily.credits}[/] credits ~${self.tavily.usd:.3f}"
        counts.add_row(f"concepts [bold]{c('concept:promoted')}[/] promoted [dim]/ {concepts} found[/]", spend)
        return Panel(Group(head, bars, counts), title=f"[bold]{escape(self.topic)}[/]", border_style="cyan", box=box.ROUNDED)


# --- final summary ------------------------------------------------------------------


def knowledge_tree(tree, max_children: int = 12) -> Tree:
    """The kept part of the knowledge tree: ran queries, read sources, promoted concepts."""
    root = Tree(f"[bold cyan]{escape(tree.root.label)}[/]")

    def keep(n) -> bool:
        return (n.kind, n.status) in {("query", "ran"), ("source", "scraped"), ("concept", "promoted")}

    def label(n) -> str:
        if n.kind == "query":
            via = {"needle_seed": "seed", "needle_concept": "concept", "problem_concept": "problem"}.get(n.via, n.via)
            return f"[blue]🔎 {escape(n.label)}[/] [dim]{n.score:.2f} · {via}[/]"
        if n.kind == "concept":
            if n.data.get("kind") == "problem":
                return f"[red]⚠ {escape(n.label)}[/]"
            return f"[yellow]💡 {escape(n.label)}[/]"
        rel = n.data.get("page_relevant")
        sid = f"[bold]S{n.data['sid']}[/] " if "sid" in n.data else ""
        rel_s = f"[{_score_style(rel)}]{rel:.2f}[/]" if rel is not None else ""
        return f"{sid}{escape(n.label[:80])} {rel_s} [dim]{_host(n.url)}[/]"

    def walk(nid: str, branch: Tree) -> None:
        kids = [tree.nodes[c] for c in tree.nodes[nid].children if keep(tree.nodes[c])]
        kids.sort(key=lambda k: -(k.data.get("page_relevant") or k.score or 0))
        for k in kids[:max_children]:
            walk(k.id, branch.add(label(k)))
        if len(kids) > max_children:
            branch.add(f"[dim]… {len(kids) - max_children} more[/]")

    walk(tree.root.id, root)
    return root


def summary(run_dir: Path, s: dict, tree=None) -> None:
    out = Console(highlight=False)
    if tree is not None:
        out.print(Panel(knowledge_tree(tree), title="[bold]knowledge tree[/]", border_style="cyan", box=box.ROUNDED))

    t = Table(box=box.SIMPLE_HEAD, show_header=True, header_style="bold")
    t.add_column("")
    t.add_column("", justify="right")
    tr = s["tree"]
    t.add_row("pages per depth", " / ".join(f"{a}/{b}" for a, b in zip(tr["pages_per_depth_spent"], tr["pages_per_depth_budget"])))
    dp = tr["delve_precision"]
    t.add_row("delve precision", f"[{'green' if dp and dp >= 0.7 else 'yellow'}]{dp}[/] ({tr['delved_pages']} delved)" if dp is not None else "—")
    t.add_row("jev", f"{s['jev']['calls']} calls · ${s['jev']['cost_usd']:.4f}")
    if s["search"]["tavily_credits"]:
        t.add_row("tavily", f"{s['search']['tavily_credits']} credits · ~${s['search']['tavily_usd_est']:.3f}")
    if s["report"]:
        r = s["report"]
        cost = f"${r['cost_usd']:.3f}" if r["cost_usd"] is not None else "?"
        t.add_row("report llm", f"{r['model']} · {r['input_tokens']:,} in / {r['output_tokens']:,} out · {cost}")
    t.add_row("[bold]total[/]", f"[bold]${s['cost_usd_total']}[/]  in {s['timings']['total_s']}s")
    t.add_row("stopped because", s["stop_reason"] or "—")
    out.print(t)

    report = run_dir / "report.md"
    if report.exists():
        out.print(f"[bold green]✔ report[/]  [link=file://{report.resolve()}]{report}[/link]")
    out.print(f"[dim]  tree     {run_dir / 'tree.md'}\n  run      {run_dir / 'run.json'}[/]")
