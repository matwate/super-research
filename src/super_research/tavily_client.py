"""Tavily as a search backend (with page text included) and as an extract fallback for
pages the plain scraper can't read (bot walls, JS-only pages, PDFs)."""

from __future__ import annotations

import os
from typing import Callable

from tavily import AsyncTavilyClient

from .scraper import Page, ScrapeError, page_from_markdown
from .searx import SearchResult

# Price per credit as of 2026-09 (confirmed by the account owner). Basic search = 1 credit,
# advanced = 2, basic extract = 1 credit per 5 URLs.
USD_PER_CREDIT = 0.008


def available() -> bool:
    return bool(os.environ.get("TAVILY_API_KEY"))


class TavilySearch:
    def __init__(
        self,
        depth: str,
        max_results: int,
        prefer_domains: tuple[str, ...],
        token_budget: int,
        log: Callable[[dict], None],
    ):
        self.client = AsyncTavilyClient()
        self.depth = depth
        self.max_results = max_results
        self.prefer_domains = list(prefer_domains)
        self.token_budget = token_budget
        self.log = log
        self.credits = 0
        self.searches = 0
        self.extracts = 0

    def _charge(self, resp: dict) -> None:
        self.credits += (resp.get("usage") or {}).get("credits", 0)

    async def search(self, query: str) -> list[SearchResult]:
        resp = await self.client.search(
            query,
            search_depth=self.depth,
            max_results=self.max_results,
            include_raw_content="markdown",
            include_domains=self.prefer_domains or None,
            # "prefer" boosts these domains without excluding blogs, docs or repos elsewhere.
            include_domains_mode="prefer" if self.prefer_domains else None,
            include_usage=True,
        )
        self.searches += 1
        self._charge(resp)
        results = [
            SearchResult(
                url=r["url"],
                title=(r.get("title") or "").strip(),
                snippet=(r.get("content") or "").strip()[:400],
                engines=["tavily"],
                provider="tavily",
                content=r.get("raw_content") or "",
            )
            for r in resp.get("results", [])
        ]
        self.log({"kind": "tavily_search", "query": query, "credits": (resp.get("usage") or {}).get("credits"), "urls": [r.url for r in results]})
        return results

    async def extract(self, url: str, title: str = "") -> Page:
        resp = await self.client.extract(url, format="markdown", include_usage=True)
        self._charge(resp)
        ok = resp.get("results") or []
        self.log({"kind": "tavily_extract", "url": url, "ok": bool(ok), "credits": (resp.get("usage") or {}).get("credits")})
        if not ok or not ok[0].get("raw_content"):
            raise ScrapeError("tavily extract: no content")
        return page_from_markdown(url, title or ok[0].get("title") or url, ok[0]["raw_content"], self.token_budget)

    @property
    def usd(self) -> float:
        return self.credits * USD_PER_CREDIT
