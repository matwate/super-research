"""Stage 7: the single prose-writing LLM call (OpenCode Go, OpenAI-compatible)."""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass

import httpx

from .context import ResearchContext
from .scraper import CHARS_PER_TOKEN
from .tree_manager import KnowledgeTree

# USD per 1M tokens (input, output) from opencode.ai/docs/go; unknown models report tokens only.
PRICES = {
    "kimi-k3": (3.00, 15.00),
    "glm-5.3": (1.40, 4.40),
    "glm-5.2": (1.40, 4.40),
    "deepseek-v4-pro": (1.32, 3.96),
    "qwen3.8-max": (2.00, 6.00),
    "qwen3.7-plus": (0.40, 1.60),
    "minimax-m3": (0.30, 1.20),
    "gpt-5.6-luna": (0.20, 1.20),
    "mimo-v2.6-pro": (0.435, 0.87),
    "glm-5.3-flash": (0.15, 0.50),
    "mimo-v2.6-flash": (0.14, 0.28),
    "deepseek-v4-flash": (0.15, 0.60),  # off-peak; peak (01-04, 06-10 UTC weekdays) is 2x
    "qwen3.8-flash": (0.15, 0.47),
}
# Reasoning some models inline in the content; an unclosed block means the answer never came.
_THINK = re.compile(r"<(think|thinking|reasoning)>.*?(</\1>|\Z)", re.S | re.I)
MIN_PAGE_RELEVANCE = 0.3  # pages Jev judged near-empty for the topic are left out
OVERHEAD_TOKENS = 6_000  # instructions, tree outline, query list

SYSTEM = """You write research reports from gathered sources. Rules:
- Use only the SOURCES provided. Cite every factual claim inline as [S#]. Never invent citations, numbers, or results.
- If sources disagree, say so and cite both. If something the intent asks for is not covered by the sources, say it is missing rather than filling it from memory; you may add clearly-labelled background ("Background, not from sources:") sparingly.
- The KNOWLEDGE TREE shows how sources were found: which query surfaced them, which page linked to which, which concepts spawned follow-up searches. Use it to explain how the field connects (lineage of methods, which works build on which) and where coverage is thin.

Structure (markdown):
# <title>
## Summary  (5-8 bullet points)
## Methods landscape  (a comparison table: method, core idea, strengths, weaknesses, sources)
## Empirical results  (benchmarks, datasets, reported numbers with citations; a table where possible)
## How the pieces connect  (derived from the knowledge tree)
## What is missing  (gaps in the literature AND gaps in this search's coverage)
## Sources  (S# - title - URL, one per line)"""


@dataclass
class ReportResult:
    markdown: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float | None
    finish_reason: str
    sources_used: int


def assemble(ctx: ResearchContext, tree: KnowledgeTree, context_tokens: int) -> tuple[str, int]:
    """Builds the user message; allocates the page-text budget by Jev relevance order.
    Unused share from short pages rolls forward to later pages."""
    pages = [p for p in tree.scraped_sources() if (p.data.get("page_relevant") or 0) >= MIN_PAGE_RELEVANCE]
    budget = max(0, context_tokens - OVERHEAD_TOKENS) * CHARS_PER_TOKEN
    blocks = []
    for i, p in enumerate(pages):
        share = budget // max(1, len(pages) - i)
        text = p.data.get("text", "")[:share]
        budget -= len(text)
        p.data["sid"] = i + 1
        q = tree.origin_query(p)
        path = " > ".join(n.label[:60] for n in tree.ancestry(p)[1:]) or "(search)"
        blocks.append(
            f"### [S{i + 1}] {p.label}\nURL: {p.url}\nFound via: {p.via} | query: {q.label if q else '-'} | path: {path}\n"
            f"Jev content relevance: {p.data.get('page_relevant', 0):.2f} | depth: {p.depth}\n\n{text}"
        )
    queries = sorted(tree.of("query"), key=lambda n: -(n.score or 0))
    qlines = "\n".join(f"- [{q.status}] {q.label} (jev {q.score:.2f}, {q.via})" for q in queries if q.score is not None)
    msg = (
        f"RESEARCH CONTEXT\n{ctx.render()}\n\n"
        f"QUERIES (Jev gate score; ran = executed)\n{qlines}\n\n"
        f"KNOWLEDGE TREE (scraped sources, promoted concepts; [S#] = source id below)\n{tree.render()}\n\n"
        f"SOURCES\n\n" + "\n\n---\n\n".join(blocks)
    )
    return msg, len(pages)


async def write(
    ctx: ResearchContext,
    tree: KnowledgeTree,
    *,
    base_url: str,
    model: str,
    context_tokens: int,
    max_output_tokens: int,
    session_id: str,
    prompt_path=None,
) -> ReportResult:
    user, n_sources = assemble(ctx, tree, context_tokens)
    if prompt_path:
        prompt_path.write_text(f"=== SYSTEM ===\n{SYSTEM}\n\n=== USER ===\n{user}")
    key = os.environ.get("OPENCODE_API_KEY")
    if not key:
        raise RuntimeError("OPENCODE_API_KEY is not set")
    body = {
        "model": model,
        "max_tokens": max_output_tokens,
        "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
    }
    headers = {
        "Authorization": f"Bearer {key}",
        "User-Agent": "super-research/0.1",
        # OpenCode Go rejects requests without a stable per-conversation session id.
        "x-opencode-session": session_id,
    }
    async with httpx.AsyncClient(timeout=httpx.Timeout(900.0, connect=20.0)) as client:
        for attempt in range(3):
            try:
                resp = await client.post(f"{base_url.rstrip('/')}/chat/completions", json=body, headers=headers)
                if resp.status_code < 500 and resp.status_code != 429:
                    break
            except httpx.TransportError:
                if attempt == 2:
                    raise
            await asyncio.sleep(5 * (attempt + 1))
        if resp.status_code >= 400:
            raise RuntimeError(f"OpenCode {resp.status_code}: {resp.text[:500]}")
    data = resp.json()
    choice = data["choices"][0]
    usage = data.get("usage") or {}
    tin, tout = usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)
    price = PRICES.get(model)
    cost = (tin * price[0] + tout * price[1]) / 1e6 if price else None
    text = strip_reasoning(choice["message"].get("content") or "")
    if not text:
        raise RuntimeError(f"empty report (finish_reason={choice.get('finish_reason')}); raise report_max_output_tokens")
    return ReportResult(text, data.get("model", model), tin, tout, cost, choice.get("finish_reason", ""), n_sources)


def strip_reasoning(text: str) -> str:
    return _THINK.sub("", text).strip()


def offline_report(ctx: ResearchContext, tree: KnowledgeTree, context_tokens: int) -> ReportResult:
    user, n = assemble(ctx, tree, context_tokens)
    md = f"# {ctx.topic} (offline dry run)\n\nNo LLM was called. Knowledge tree:\n\n{tree.render()}\n"
    return ReportResult(md, "offline", len(user) // CHARS_PER_TOKEN, 0, 0.0, "offline", n)
