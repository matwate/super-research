"""SearXNG JSON API wrapper."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import httpx


@dataclass
class SearchResult:
    url: str
    title: str
    snippet: str
    engines: list[str]
    provider: str = "searxng"
    content: str = ""  # full page text when the provider already fetched it (Tavily)


async def search(
    client: httpx.AsyncClient,
    base_url: str,
    query: str,
    engines: tuple[str, ...],
    limit: int,
) -> tuple[list[SearchResult], list[str]]:
    """Returns (results deduped by URL, unresponsive engine notes)."""
    params = {"q": query, "format": "json"}
    if engines:
        params["engines"] = ",".join(engines)
    for attempt in range(2):  # one retry: timeouts are usually a transient limiter stall
        try:
            resp = await client.get(f"{base_url.rstrip('/')}/search", params=params, timeout=45.0)
            resp.raise_for_status()
            break
        except (httpx.TimeoutException, httpx.HTTPStatusError):
            if attempt:
                raise
            await asyncio.sleep(3)
    data = resp.json()
    seen: set[str] = set()
    out: list[SearchResult] = []
    for r in data.get("results", []):
        url = r.get("url")
        if not url or url in seen:
            continue
        seen.add(url)
        out.append(
            SearchResult(
                url=url,
                title=(r.get("title") or "").strip(),
                snippet=(r.get("content") or "").strip()[:400],
                engines=r.get("engines") or [r.get("engine", "")],
            )
        )
        if len(out) >= limit:
            break
    dead = [f"{e}: {why}" for e, why in data.get("unresponsive_engines", [])]
    return out, dead
