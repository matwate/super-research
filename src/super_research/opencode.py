"""OpenCode Go's live model list (OpenAI-compatible GET /models).

The Go lineup changes often, so the report and drafter models are checked against it
before a pass spends anything, and the web UI fills its dropdowns from it.
"""

from __future__ import annotations

import hashlib
import time

import httpx

CACHE_SECONDS = 600
_cache: dict[tuple[str, str], tuple[float, list[str]]] = {}


async def model_ids(base_url: str, api_key: str | None, refresh: bool = False) -> list[str]:
    """Model ids the endpoint offers now, cached for a few minutes per key."""
    key_hash = hashlib.sha256((api_key or "").encode()).hexdigest()
    cache_key = (base_url, key_hash)
    hit = _cache.get(cache_key)
    if hit and not refresh and time.monotonic() - hit[0] < CACHE_SECONDS:
        return hit[1]
    headers = {"User-Agent": "super-research/0.1"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10.0)) as client:
        resp = await client.get(f"{base_url.rstrip('/')}/models", headers=headers)
    if resp.status_code >= 400:
        raise RuntimeError(f"OpenCode models {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    items = data.get("data", data) if isinstance(data, dict) else data
    ids = sorted({str(m["id"]) for m in items if isinstance(m, dict) and m.get("id")})
    if not ids:
        raise RuntimeError("OpenCode models: empty list")
    _cache[cache_key] = (time.monotonic(), ids)
    return ids


def missing_model_message(field: str, model: str, ids: list[str]) -> str:
    close = [m for m in ids if m.split("-")[0] == model.split("-")[0]]
    hint = f" Close matches: {', '.join(close)}." if close else ""
    return f"{field} {model!r} is not on OpenCode Go right now.{hint} Available: {', '.join(ids)}"
