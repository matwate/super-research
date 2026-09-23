"""Stage 1: a cheap LLM drafts the seed queries.

Needle can't invent queries (it only copies spans from its input), so seeds came out as
"<topic> survey", "<topic> arxiv", ... A flash model on OpenCode Go knows the field's
vocabulary ("SEIR", "stiff ODE", "DeepXDE") and costs ~$0.0001 per pass. Jev still gates
every query, and the template facets are the fallback if the call fails.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass

import httpx

from .context import ResearchContext
from .report import PRICES, strip_reasoning

PROMPT = """{context}

Write {n} diverse web search queries that together cover this research: key methods and model families, known technical problems and failure modes, benchmarks and datasets, recent ({year}) work, surveys, and code. Use the field's own vocabulary and synonyms, and spell out acronyms in some queries. Each query 3-8 words, no quotes or search operators.
Return only JSON: {{"queries": ["..."]}}"""


@dataclass
class Draft:
    queries: list[str]
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float | None


def parse(text: str, n: int) -> list[str]:
    text = strip_reasoning(text)
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return []
    try:
        raw = json.loads(m.group(0)).get("queries", [])
    except json.JSONDecodeError:
        return []
    out = [" ".join(str(q).split()).strip(" .\"'") for q in raw if isinstance(q, str)]
    return list(dict.fromkeys(q for q in out if 2 <= len(q) <= 120))[:n]


async def draft(ctx: ResearchContext, n: int, *, base_url: str, model: str, session_id: str, year: int) -> Draft:
    key = os.environ.get("OPENCODE_API_KEY")
    if not key:
        raise RuntimeError("OPENCODE_API_KEY is not set")
    body = {
        "model": model,
        "max_tokens": 4000,  # reasoning models spend tokens thinking before the JSON
        "messages": [{"role": "user", "content": PROMPT.format(context=ctx.render(), n=n, year=year)}],
    }
    headers = {"Authorization": f"Bearer {key}", "User-Agent": "super-research/0.1", "x-opencode-session": session_id}
    async with httpx.AsyncClient(timeout=httpx.Timeout(90.0, connect=15.0)) as client:
        resp = await client.post(f"{base_url.rstrip('/')}/chat/completions", json=body, headers=headers)
    if resp.status_code >= 400:
        raise RuntimeError(f"drafter {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    usage = data.get("usage") or {}
    tin, tout = usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)
    price = PRICES.get(model)
    queries = parse(data["choices"][0]["message"].get("content") or "", n)
    if not queries:
        raise RuntimeError("drafter returned no parseable queries")
    return Draft(queries, data.get("model", model), tin, tout, (tin * price[0] + tout * price[1]) / 1e6 if price else None)
