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
    # A page's own content must score at least this before its links are considered.
    # Stops the crawl drifting off-topic through pages that turned out to be irrelevant.
    page_for_delve: float = 0.4
    # Frontier priority = score * depth_decay ** (depth - 1): prefer shallow nodes a bit.
    depth_decay: float = 0.85


@dataclass(frozen=True)
class Settings:
    budgets: Budgets = field(default_factory=Budgets)
    gates: Gates = field(default_factory=Gates)
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
    searx_url: str = "https://search.matwa.dev"
    # DuckDuckGo/Brave/Startpage/Google get captcha'd or return nothing on the instance;
    # these answer reliably (arxiv/semantic scholar are flaky but fail fast).
    searx_engines: tuple[str, ...] = ("bing", "google scholar", "crossref", "arxiv", "semantic scholar")
    # Parallel bursts against the instance time out (its limiter / upstream engines),
    # so searches are throttled separately from scraping.
    searx_concurrency: int = 2
    jev_model: str = "jev-latest"
    report_model: str = "minimax-m3"
    opencode_url: str = "https://opencode.ai/zen/go/v1"
    concurrency: int = 8
    reports_dir: Path = Path("reports")
    offline: bool = False  # stub Jev + skip network for tests / dry runs


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
    return _coerce(Settings, replace(settings, budgets=budgets, gates=gates), patch)


def load(
    preset: str = "standard",
    config_file: Path | None = None,
    overrides: dict[str, Any] | None = None,
) -> Settings:
    if preset not in PRESETS:
        raise ValueError(f"unknown preset {preset!r}; choose from {sorted(PRESETS)}")
    s = apply(Settings(), PRESETS[preset])
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
