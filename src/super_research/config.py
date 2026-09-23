"""Run settings. Every budget and gate is tunable from a preset, a TOML file, or CLI flags.

Precedence (later wins): defaults -> preset -> research.toml -> --config FILE -> CLI flags.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Budgets:
    # Pages scraped per hop depth. Depth 1 = pages reached from search results,
    # depth 2 = links delved from depth-1 pages, and so on. len() is the max depth.
    pages_per_depth: tuple[int, ...] = (30, 15, 8)
    # Tokens of extracted text kept per page (approx. 4 chars per token).
    page_tokens: int = 4000
    # Anchors shown to Jev per page after code-side filtering.
    anchors_per_page: int = 60
    # Max links Jev may approve for delving from a single page.
    delve_per_page: int = 5
    # SearXNG results kept per query (merged across engines). Generous on purpose: web
    # engines return junk for ambiguous terms and Jev rating 20 results costs ~$0.0002.
    results_per_query: int = 20
    # Needle drafts this many seed queries (the facet plan is truncated to it).
    seed_queries: int = 10
    # Concept expansion: after a scrape wave, Needle extracts named concepts from
    # the best pages; they become candidate queries for another search round.
    expansion_rounds: int = 1
    expansion_queries: int = 6
    # Of which extra searches for problem phrases ("gradient imbalance") per round.
    expansion_problem_queries: int = 3
    # Share of the depth-1 page budget held back from seed results so concept branches
    # still have room to be scraped. 0 lets seeds take everything.
    expansion_reserve: float = 0.3
    # Hard caps on the whole pass.
    max_jev_calls: int = 400
    max_seconds: int = 600
    # Tokens of page context handed to the final LLM.
    report_context_tokens: int = 50_000
    report_max_output_tokens: int = 16_000

    @property
    def max_depth(self) -> int:
        return len(self.pages_per_depth)


@dataclass(frozen=True)
class Gates:
    # All gates are Jev noul probabilities in [0, 1]. Tune toward 0.5 if the frontier collapses.
    query: float = 0.5  # run a drafted query
    relevance: float = 0.5  # scrape a search result
    # Follow a link from a scraped page. 0.5 let through too many repo subfolders and
    # DOI landing pages in testing (delve precision 0.58); 0.6 keeps nearly all good ones.
    delve: float = 0.6
    concept: float = 0.5  # turn an extracted concept into a search
    problem: float = 0.5  # turn an extracted problem phrase into a search
    # A page's own content must score at least this before its links are considered.
    # Stops the crawl drifting off-topic through pages that turned out to be irrelevant.
    page_for_delve: float = 0.4
    # Frontier priority = score * depth_decay ** (depth - 1): prefer shallow nodes a bit.
    depth_decay: float = 0.85


@dataclass(frozen=True)
class Template:
    """The stage-0 starting template: how a raw topic becomes the research context and the
    facet plan (the first branches of the knowledge tree when no drafter runs, and the
    context the drafter, Jev and the report LLM all read).

    Placeholders: {topic}, {core} (topic minus deliverable words), {year}, {last_year},
    {focus} (", including A, B" when --focus terms are given, else empty). Other braces
    are left as typed."""

    intent: str = "find papers, benchmarks, and engineering writeups covering {topic}{focus}, plus any {year} updates."
    deliverable: str = "a comparison of methods, empirical results, plus what is missing."
    tone: str = "research oriented"
    filter: str = "skip product pages, skip tutorials below graduate level, skip SEO listicles."
    facets: tuple[str, ...] = (
        "{core}",
        "{core} survey",
        "{core} benchmark comparison",
        "{core} arxiv",
        "{core} {year}",
        "{core} limitations",
        "{core} github implementation",
        "{core} explained blog",
        "{core} state of the art",
        "{core} ablation study",
        "{core} review {last_year}",
    )
    # --- the lens: what Jev, the drafter and the report LLM treat as useful ---------
    # What a good source looks like (Jev result + link criteria).
    sources: str = "a research paper, benchmark, survey, technical blog post, code repository, or documentation"
    # What to reject besides `filter` (Jev result criteria).
    avoid: str = "a product or marketing page, a shallow or beginner tutorial, a listing page with no content of its own"
    # What counts as a named concept worth its own search (Jev concept gate).
    concepts: str = "method, algorithm, model, dataset, or benchmark"
    # What the seed queries should cover together (drafter prompt).
    draft_focus: str = "key methods and model families, known technical problems and failure modes, benchmarks and datasets, recent ({year}) work, surveys, and code"
    # Markdown outline of the report, and extra writing rules for the report LLM.
    report_outline: str = """# <title>
## Summary  (5-8 bullet points)
## Methods landscape  (a comparison table: method, core idea, strengths, weaknesses, sources)
## Empirical results  (benchmarks, datasets, reported numbers with citations; a table where possible)
## How the pieces connect  (derived from the knowledge tree)
## What is missing  (gaps in the literature AND gaps in this search's coverage)
## Sources  (S# - title - URL, one per line)"""
    report_rules: str = ""
    # Tavily: preferred domains (empty = settings.tavily_prefer_domains) and recency
    # ("" | "day" | "week" | "month" | "year").
    prefer_domains: tuple[str, ...] = ()
    time_range: str = ""
    # SearXNG engines for this template (empty = settings.searx_engines).
    searx_engines: tuple[str, ...] = ()


# --- starting templates -----------------------------------------------------------------
# Market research: "is X worth investing in", "what's trending", asset-class studies
# (stocks, funds, collectibles). The report presents evidence and both cases; it never
# issues personal buy/sell calls, and every number carries an as-of date.
_MARKET_RULES = (
    "Attach an as-of date to every price, return, valuation, forecast or ranking, and say when a figure may be stale"
    " | Separate reported facts from opinions and forecasts, and name who holds each opinion"
    " | For every backtest or return study, state the period, the method, and known biases (survivorship,"
    " look-ahead, selection, fees and transaction costs, spreads, liquidity) before its result"
    " | Do not give personal buy, sell or hold instructions or price targets of your own; present the evidence"
    " and what would have to be true for each case"
    " | End the report with one line: This is research, not financial advice."
)
_MARKET_DOMAINS = (
    "sec.gov",
    "reuters.com",
    "bloomberg.com",
    "ft.com",
    "wsj.com",
    "cnbc.com",
    "morningstar.com",
    "marketwatch.com",
    "finance.yahoo.com",
    "seekingalpha.com",
    "ssrn.com",
    "nber.org",
)
_MARKET_LENS = dict(
    sources=(
        "a financial filing or earnings report, an analyst or equity research note, a reputable financial news"
        " article, a market data or price-history page, an academic or industry study, or a backtest with a stated method"
    ),
    avoid=(
        "a promotional or sponsored pick, a hype or pump post, a page with no data or no dates, a broker sign-up or"
        " trading-app landing page"
    ),
    concepts="company, ticker, fund, index, asset class, product line, or named study, index or dataset",
    report_rules=_MARKET_RULES,
    prefer_domains=_MARKET_DOMAINS,
)

MARKET = Template(
    intent=(
        "evaluate {topic}{focus} as an investment: fundamentals or value drivers, valuation and pricing, recent"
        " performance, catalysts and risks, analyst and academic views, and historical or backtested returns, plus"
        " any {year} developments."
    ),
    deliverable=(
        "an evidence-based investment case: bull and bear arguments, key numbers with dates, historical and"
        " backtested performance with its caveats, risks, plus what the evidence cannot tell you."
    ),
    tone="analytical and skeptical, like an independent research note",
    filter="skip sponsored picks, hype and pump posts, SEO listicles, and undated price predictions.",
    facets=(
        "{core} investment analysis",
        "{core} returns historical performance",
        "{core} backtest",
        "{core} investment study",
        "{core} valuation",
        "{core} risks",
        "{core} outlook {year}",
        "{core} news {year}",
        "{core} market data price history",
        "{core} vs alternatives comparison",
    ),
    draft_focus=(
        "value drivers and valuation, recent results and news, catalysts and risks, analyst and academic views,"
        " historical returns and backtests, comparable assets or benchmarks, and the most recent ({year}) data"
    ),
    report_outline="""# <title>
## Bottom line  (3-6 bullets: what the evidence says, not a buy/sell instruction)
## Snapshot  (table: metric, value, as-of date, source)
## Bull case
## Bear case and risks
## Historical performance and backtests  (table: study or test, period, method, result, biases, source)
## What analysts, markets and studies say  (where they agree and disagree)
## How the pieces connect  (derived from the knowledge tree)
## What is missing or stale
## Sources  (S# - title - URL, one per line)""",
    **_MARKET_LENS,
)

MARKET_TRENDING = Template(
    intent=(
        "find which {topic}{focus} are trending now and why: price moves, volume, news catalysts, sentiment, and"
        " what the evidence says about each, plus how reliable such trends have been historically."
    ),
    deliverable=(
        "a list of what is trending with the reason, key numbers with dates, and the main risk for each, plus"
        " evidence on whether chasing these trends has paid off."
    ),
    tone="analytical and skeptical, like an independent market brief",
    filter="skip sponsored picks, hype and pump posts, SEO listicles, and undated price predictions.",
    facets=(
        "{core} trending this week",
        "{core} top gainers {year}",
        "{core} most active volume",
        "{core} news catalyst",
        "{core} analyst upgrades downgrades",
        "{core} momentum",
        "{core} sentiment",
        "momentum investing backtest evidence",
    ),
    draft_focus=(
        "what is moving right now and why, volume and momentum, news catalysts, analyst changes, sentiment,"
        " and evidence from studies and backtests on whether trend-following pays off"
    ),
    report_outline="""# <title>
## Bottom line  (3-6 bullets, not a buy/sell instruction)
## Trending now  (table: name or ticker, move, why, as-of date, main risk, sources)
## Catalysts and drivers
## Does chasing this pay off?  (momentum and trend-following evidence: study, period, method, result, biases)
## Risks
## How the pieces connect  (derived from the knowledge tree)
## What is missing or stale
## Sources  (S# - title - URL, one per line)""",
    time_range="week",
    **_MARKET_LENS,
)

# Selectable with --template NAME (and listed in the web UI). "research" is the default.
TEMPLATES: dict[str, Template] = {
    "research": Template(),
    "market": MARKET,
    "market-trending": MARKET_TRENDING,
}


@dataclass(frozen=True)
class Settings:
    budgets: Budgets = field(default_factory=Budgets)
    gates: Gates = field(default_factory=Gates)
    template: Template = field(default_factory=Template)
    # Backends queried per search, merged by URL (first listed wins on duplicates).
    # "auto" = tavily + searxng when TAVILY_API_KEY is set, else searxng only.
    search_backends: tuple[str, ...] = ("auto",)
    tavily_depth: str = "advanced"  # basic = 1 credit, advanced = 2 (much better on ambiguous topics)
    tavily_max_results: int = 10
    # Preferred (boosted, not exclusive) domains for research-oriented searches.
    tavily_prefer_domains: tuple[str, ...] = (
        "arxiv.org",
        "openreview.net",
        "proceedings.neurips.cc",
        "proceedings.mlr.press",
        "aclanthology.org",
        "github.com",
    )
    # When the plain scraper fails (bot wall, JS-only, HTTP 4xx), retry the URL via Tavily extract.
    tavily_extract_fallback: bool = True
    # Extracts are charged even when they fail, so only blocked/JS-only pages qualify
    # (not 404s or non-HTML), and at most this many per pass.
    tavily_extract_max: int = 8
    searx_url: str = "https://search.matwa.dev"
    # DuckDuckGo/Brave/Startpage/Google get captcha'd or return nothing on the instance;
    # these answer reliably (arxiv/semantic scholar are flaky but fail fast).
    searx_engines: tuple[str, ...] = ("bing", "google scholar", "crossref", "arxiv", "semantic scholar")
    # Parallel bursts against the instance time out (its limiter / upstream engines),
    # so searches are throttled separately from scraping.
    searx_concurrency: int = 2
    jev_model: str = "jev-latest"
    report_model: str = "minimax-m3"
    # Drafts the seed queries (stage 1). Picked on quality in a 4-way test; all flash
    # models cost ~$0.0001 per pass. "" = skip it and use the template facets.
    draft_model: str = "glm-5.3-flash"
    opencode_url: str = "https://opencode.ai/zen/go/v1"
    concurrency: int = 8
    reports_dir: Path = Path("reports")
    offline: bool = False  # stub Jev + skip network for tests / dry runs


@dataclass(frozen=True)
class Keys:
    """API keys for one run. Passed explicitly (not via os.environ) so the web server can
    run several users' passes at once, each with the keys they brought. Never written to
    run.json."""

    opencode: str | None = None
    typesafe: str | None = None
    tavily: str | None = None

    @classmethod
    def from_env(cls) -> "Keys":
        return cls(
            opencode=os.environ.get("OPENCODE_API_KEY") or None,
            typesafe=os.environ.get("TYPESAFE_API_KEY") or None,
            tavily=os.environ.get("TAVILY_API_KEY") or None,
        )

    def __repr__(self) -> str:  # keep keys out of logs and tracebacks
        return f"Keys(opencode={bool(self.opencode)}, typesafe={bool(self.typesafe)}, tavily={bool(self.tavily)})"


PRESETS: dict[str, dict[str, Any]] = {
    "quick": {
        "budgets": {
            "pages_per_depth": [10, 4],
            "seed_queries": 6,
            "expansion_rounds": 1,
            "expansion_queries": 4,
            "max_jev_calls": 120,
            "max_seconds": 180,
            "report_context_tokens": 25_000,
        }
    },
    "standard": {},
    "deep": {
        "budgets": {
            "pages_per_depth": [50, 30, 15, 6],
            "seed_queries": 12,
            "expansion_rounds": 2,
            "expansion_queries": 8,
            "max_jev_calls": 1000,
            "max_seconds": 1500,
            "report_context_tokens": 90_000,
        }
    },
}


def _coerce(cls: type, current: Any, patch: dict[str, Any]) -> Any:
    known = {f.name for f in fields(cls)}
    unknown = set(patch) - known
    if unknown:
        raise ValueError(f"unknown {cls.__name__} keys: {sorted(unknown)}")
    clean = {}
    for k, v in patch.items():
        if isinstance(v, list):
            v = tuple(v)
        if k.endswith("_dir") and isinstance(v, str):
            v = Path(v)
        clean[k] = v
    return replace(current, **clean)


def apply(settings: Settings, patch: dict[str, Any]) -> Settings:
    patch = dict(patch)
    budgets = _coerce(Budgets, settings.budgets, patch.pop("budgets", {}))
    gates = _coerce(Gates, settings.gates, patch.pop("gates", {}))
    template = _coerce(Template, settings.template, patch.pop("template", {}))
    return _coerce(Settings, replace(settings, budgets=budgets, gates=gates, template=template), patch)


def load(
    preset: str = "standard",
    config_file: Path | None = None,
    overrides: dict[str, Any] | None = None,
    template: str = "research",
) -> Settings:
    if preset not in PRESETS:
        raise ValueError(f"unknown preset {preset!r}; choose from {sorted(PRESETS)}")
    if template not in TEMPLATES:
        raise ValueError(f"unknown template {template!r}; choose from {sorted(TEMPLATES)}")
    s = replace(apply(Settings(), PRESETS[preset]), template=TEMPLATES[template])
    for path in (Path("research.toml"), config_file):
        if path and path.exists():
            s = apply(s, tomllib.loads(path.read_text()))
        elif path and path is config_file:
            raise FileNotFoundError(path)
    env = {
        "searx_url": os.environ.get("SEARXNG_URL"),
        "report_model": os.environ.get("RESEARCH_MODEL"),
    }
    s = apply(s, {k: v for k, v in env.items() if v})
    return apply(s, overrides or {})
