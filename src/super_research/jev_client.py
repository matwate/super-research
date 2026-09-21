"""Jev (TypeSafe System One) as the pipeline's decision layer.

Every decision is a Noul per item, fanned out in one request over shared state, so the
state is ingested once and all items are judged in parallel. Code owns thresholds.
`StubJudge` implements the same interface with keyword overlap for offline tests.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any, Callable, Protocol

from typesafe_sdk import AsyncTypeSafeClient, Noul, NoulCriteria

from .scraper import Anchor, Page
from .searx import SearchResult

JEV_PRICE_PER_MTOK = 0.042  # input tokens; output is free
MAX_QUESTIONS_PER_CALL = 50


class JevBudgetExceeded(Exception):
    pass


class Judge(Protocol):
    calls: int
    input_tokens: int

    async def gate_queries(self, research: dict, queries: list[str], already: list[str]) -> list[float]: ...
    async def rate_results(self, research: dict, query: str, results: list[SearchResult]) -> list[float]: ...
    async def page_relevance(self, research: dict, page: Page) -> float: ...
    async def rate_links(self, research: dict, page: Page, anchors: list[Anchor]) -> list[float]: ...
    async def gate_concepts(self, research: dict, concepts: list[tuple[str, int]]) -> list[float]: ...


# Criteria are spelled out because Jev reads questions literally.
_Q_QUERY = NoulCriteria(
    true="The query is specific enough to return papers, benchmarks, results or technical writeups that serve `research.intent`.",
    false="The query is off-topic, too vague to return useful sources, targets content excluded by `research.filter`, or asks for the same thing as an entry in `already_searched`.",
)
_Q_RESULT = NoulCriteria(
    true="The title, snippet and URL indicate a research paper, benchmark, survey, technical blog post, or documentation about the research topic.",
    false="Off-topic, a product or marketing page, a shallow or beginner tutorial, a listing page with no content of its own, or content excluded by `research.filter`.",
)
_Q_PAGE = NoulCriteria(
    true="The text discusses the research topic with substance: methods, results, comparisons, analysis, or technical detail.",
    false="The text is off-topic, mostly navigation or boilerplate, a paywall or error page, or only mentions the topic in passing.",
)
_Q_LINK = NoulCriteria(
    true="The link text or URL points to a related paper, method, benchmark, dataset, code repository, or in-depth technical writeup on the research topic.",
    false="Navigation, site chrome, an author or profile page, a generic listing, an ad or product, or an unrelated topic.",
)
_Q_CONCEPT = NoulCriteria(
    true="A specific named method, algorithm, model, dataset, or benchmark that is part of the research topic.",
    false="A broad field or discipline name (such as Machine Learning or Computer Vision), a generic phrase, a sentence fragment, a citation fragment such as an author name and year, or something unrelated to the research topic.",
)


class JevJudge:
    def __init__(self, model: str, max_calls: int, log: Callable[[dict], None], concurrency: int = 8):
        self.client = AsyncTypeSafeClient(model=model)
        self.max_calls = max_calls
        self.log = log
        self.calls = 0
        self.input_tokens = 0
        self._sem = asyncio.Semaphore(concurrency)

    async def close(self) -> None:
        await self.client.aclose()

    async def _nouls(self, kind: str, state: Any, questions: dict[str, Noul], items: list[Any]) -> dict[str, float]:
        out: dict[str, float] = {}
        keys = list(questions)
        for i in range(0, len(keys), MAX_QUESTIONS_PER_CALL):
            if self.calls >= self.max_calls:
                raise JevBudgetExceeded(f"Jev call budget ({self.max_calls}) spent")
            self.calls += 1
            chunk = {k: questions[k] for k in keys[i : i + MAX_QUESTIONS_PER_CALL]}
            t = time.monotonic()
            async with self._sem:
                resp = await self.client.system_one(state=state, questions=chunk)
            self.input_tokens += resp.usage.input_tokens
            got = {k: resp.nouls[k].noul for k in chunk}
            out.update(got)
            self.log(
                {
                    "kind": kind,
                    "model": resp.model,
                    "ms": int((time.monotonic() - t) * 1000),
                    "input_tokens": resp.usage.input_tokens,
                    "verdicts": [{"item": items[int(k[1:])], "p": v} for k, v in got.items()],
                }
            )
        return out

    async def gate_queries(self, research, queries, already):
        state = {"research": research, "candidate_queries": queries, "already_searched": already}
        qs = {
            f"q{i}": Noul(instructions=f"Should the web search `candidate_queries[{i}]` be run to gather sources for `research`?", criteria=_Q_QUERY)
            for i in range(len(queries))
        }
        r = await self._nouls("gate_queries", state, qs, queries)
        return [r[f"q{i}"] for i in range(len(queries))]

    async def rate_results(self, research, query, results):
        items = [{"title": r.title, "url": r.url, "snippet": r.snippet} for r in results]
        state = {"research": research, "search_query": query, "results": items}
        qs = {
            f"r{i}": Noul(instructions=f"Does the search result `results[{i}]` likely lead to a page with substantive content that serves `research.intent`?", criteria=_Q_RESULT)
            for i in range(len(items))
        }
        r = await self._nouls("rate_results", state, qs, [i["url"] for i in items])
        return [r[f"r{i}"] for i in range(len(items))]

    async def page_relevance(self, research, page):
        state = {"research": research, "page": {"title": page.title, "url": page.url, "text": page.text}}
        qs = {"p0": Noul(instructions="Does `page.text` contain substantive information that serves `research.intent`?", criteria=_Q_PAGE)}
        r = await self._nouls("page_relevance", state, qs, [page.url])
        return r["p0"]

    async def rate_links(self, research, page, anchors):
        if not anchors:
            return []
        links = [{"text": a.text, "url": a.url} for a in anchors]
        state = {"research": research, "page": {"title": page.title, "url": page.url}, "links": links}
        qs = {
            f"l{i}": Noul(instructions=f"Would following `links[{i}]` from `page` likely lead to a source with substantive content that serves `research.intent`?", criteria=_Q_LINK)
            for i in range(len(links))
        }
        r = await self._nouls("rate_links", state, qs, [a.url for a in anchors])
        return [r[f"l{i}"] for i in range(len(links))]

    async def gate_concepts(self, research, concepts):
        items = [{"term": t, "mentioned_on_pages": n} for t, n in concepts]
        state = {"research": research, "concepts": items}
        qs = {
            f"c{i}": Noul(instructions=f"Is `concepts[{i}].term` a specific named method, model, dataset or benchmark worth searching for to serve `research.intent`?", criteria=_Q_CONCEPT)
            for i in range(len(items))
        }
        r = await self._nouls("gate_concepts", state, qs, [t for t, _ in concepts])
        return [r[f"c{i}"] for i in range(len(items))]


class StubJudge:
    """Offline judge: keyword overlap with the topic. Deterministic, free, dumb."""

    def __init__(self, log: Callable[[dict], None] = lambda _: None):
        self.log = log
        self.calls = 0
        self.input_tokens = 0

    @staticmethod
    def _words(s: str) -> set[str]:
        return {w for w in re.findall(r"[a-z0-9]+", s.lower()) if len(w) > 2}

    def _score(self, research: dict, text: str) -> float:
        topic = self._words(research.get("topic", ""))
        if not topic:
            return 0.5
        hit = len(topic & self._words(text)) / len(topic)
        return round(min(0.95, 0.1 + hit), 3)

    def _tick(self, kind: str, items: list, scores: list[float]) -> list[float]:
        self.calls += 1
        self.log({"kind": kind, "stub": True, "verdicts": [{"item": i, "p": p} for i, p in zip(items, scores)]})
        return scores

    async def gate_queries(self, research, queries, already):
        return self._tick("gate_queries", queries, [self._score(research, q) for q in queries])

    async def rate_results(self, research, query, results):
        return self._tick("rate_results", [r.url for r in results], [self._score(research, f"{r.title} {r.snippet}") for r in results])

    async def page_relevance(self, research, page):
        return self._tick("page_relevance", [page.url], [self._score(research, page.text)])[0]

    async def rate_links(self, research, page, anchors):
        return self._tick("rate_links", [a.url for a in anchors], [self._score(research, a.text) for a in anchors])

    async def gate_concepts(self, research, concepts):
        return self._tick("gate_concepts", [t for t, _ in concepts], [0.6 if len(t) < 40 else 0.2 for t, _ in concepts])
