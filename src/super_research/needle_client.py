"""Cactus Needle 3, run in-process (tiny local model, grammar-constrained tool calls).

Needle is a splitter/extractor: it grounds every argument in the text it is given and
refuses rather than invents. So it is used for two jobs it is good at:
  * split the templated search plan into individual, well-formed queries (stage 1)
  * pull named methods / datasets / benchmarks out of scraped pages, which seed new
    branches of the knowledge tree (concept expansion)
"""

from __future__ import annotations

import asyncio
import os
import re
import threading
import time
from typing import Callable

from .schemas import SEARCH_TOOL, TERMS_TOOL

os.environ.setdefault("NEEDLE_TELEMETRY", "0")
os.environ.setdefault("DO_NOT_TRACK", "1")

TERM_PARAGRAPHS = 2  # Needle runs on the paragraphs densest in named things
PARAGRAPH_CHARS = 600  # Needle refuses or grabs boilerplate on long inputs; short ones work
# Named-thing shapes: PCGrad, Nash-MTL, NYUv2, MGDA, CAGrad, GPT-4o, ResNet-50.
_NAMED = re.compile(r"\b(?:[A-Z][a-z]+[A-Z][A-Za-z0-9]*|[A-Z]{2,}[a-z]*[A-Za-z0-9]*|[A-Za-z]+[0-9][A-Za-z0-9]*)(?:-[A-Za-z0-9]+)*\b")
_GENERIC = {
    "PDF", "HTML", "URL", "API", "GPU", "CPU", "AI", "ML", "DOI", "USA", "IEEE", "ACM", "PMLR", "HTTP", "FAQ",
    "RSS", "JSON", "CSS", "PhD", "LaTeX", "GitHub", "arXiv", "NeurIPS", "ICML", "ICLR", "CVPR", "AAAI", "OK",
    # Site, publisher and repo chrome that shows up in scraped pages.
    "README", "PMC", "PMCID", "PMID", "ORCID", "DevOps", "EXPLORE", "SIAM", "PLOS", "MDPI", "ISSN", "ISBN",
    "PubMed", "CrossRef", "Elsevier", "Springer", "Wiley", "ResearchGate", "LinkedIn", "YouTube",
    "AI/ML", "USD", "EUR", "CC-BY", "BibTeX", "NIH", "NSF", "ACL", "EMNLP", "AAAS", "UTC",
}
_GENERIC_L = {g.lower() for g in _GENERIC}
# Reference/figure anchors and file names: bib7, ref12, fig3, eq(4), README.md, train.py
_CHROME_TERM = re.compile(r"^(bib|ref|fig|figure|table|tab|eq|sec|app|cr|b)\d+[a-z]?$|\.(md|py|txt|pdf|json|yaml|yml|ipynb)$", re.I)


def regex_terms(text: str, limit: int = 10) -> list[str]:
    """Code-side candidates: tokens shaped like method/dataset names, by frequency."""
    counts: dict[str, int] = {}
    for m in _NAMED.findall(text):
        if m.lower() in _GENERIC_L or len(m) < 3 or m.isdigit():
            continue
        counts[m] = counts.get(m, 0) + 1
    return sorted(counts, key=lambda k: -counts[k])[:limit]


def dense_paragraphs(text: str, n: int = TERM_PARAGRAPHS) -> list[str]:
    paras = [p.strip() for p in text.split("\n") if 80 < len(p.strip()) and not p.startswith("#")]
    scored = sorted(paras, key=lambda p: -len(_NAMED.findall(p)))
    return [p[:PARAGRAPH_CHARS] for p in scored[:n] if _NAMED.search(p)]


class NeedleClient:
    def __init__(self, log: Callable[[dict], None]):
        import needle  # heavy-ish: loads the engine binary on first agent

        self._needle = needle
        self._lock = threading.Lock()  # one engine; serialize calls
        self._search = None
        self._terms = None
        self.log = log
        self.calls = 0

    def _agent(self, which: str):
        if which == "search":
            if self._search is None:
                self._search = self._needle.Needle(tools=[SEARCH_TOOL])
            return self._search
        if self._terms is None:
            self._terms = self._needle.Needle(tools=[TERMS_TOOL])
        return self._terms

    def _complete(self, which: str, text: str) -> dict:
        with self._lock:
            agent = self._agent(which)
            agent.reset()
            t = time.monotonic()
            r = agent.complete(text)
            self.calls += 1
        calls = r.get("function_calls") or r.get("suppressed_calls") or []
        self.log(
            {
                "kind": which,
                "ms": int((time.monotonic() - t) * 1000),
                "confidence": r.get("confidence"),
                "suppressed": not r.get("function_calls") and bool(r.get("suppressed_calls")),
                "calls": [c.get("arguments") for c in calls],
                "reasoning": r.get("reasoning"),
                "input": text[:500],
            }
        )
        return {"calls": calls, "confidence": r.get("confidence")}

    def _draft(self, prompt: str) -> list[str]:
        r = self._complete("search", prompt)
        qs = [c["arguments"].get("query", "") for c in r["calls"] if c.get("name") == "search_web"]
        return [q for q in (clean_query(q) for q in qs) if q]

    async def draft_queries(self, chunks: list[tuple[str, list[str]]]) -> tuple[list[str], str]:
        """Returns (queries, provenance). Each plan chunk is retried once when Needle
        returns too little, then falls back to the chunk itself so an engine or grammar
        failure never stalls the run."""
        out: list[str] = []
        sources: list[str] = []
        for prompt, expected in chunks:
            for attempt in (1, 2):
                try:
                    qs = await asyncio.to_thread(self._draft, prompt)
                except Exception as e:
                    self.log({"kind": "search", "error": repr(e), "attempt": attempt})
                    qs = []
                qs = list(dict.fromkeys(qs))
                if len(qs) >= max(1, (len(expected) + 1) // 2):
                    out += qs
                    sources.append(f"needle#{attempt}")
                    break
            else:
                out += expected
                sources.append("fallback")
        return list(dict.fromkeys(out)), ",".join(sources)

    def _terms_sync(self, text: str) -> list[str]:
        terms: list[str] = []
        for para in dense_paragraphs(text):
            r = self._complete("terms", para)
            for c in r["calls"]:
                terms += c.get("arguments", {}).get("terms", []) or []
        return terms

    async def extract_terms(self, title: str, text: str, topic: str) -> list[str]:
        """Needle spans from the name-dense paragraphs, plus regex candidates. Both are
        noisy on purpose: Jev's concept gate does the selecting."""
        try:
            raw = await asyncio.to_thread(self._terms_sync, text)
        except Exception as e:
            self.log({"kind": "terms", "error": repr(e)})
            raw = []
        return clean_terms(raw + regex_terms(f"{title}\n{text}"), topic)


def clean_query(q: str) -> str:
    q = " ".join(q.split()).strip(" .,;:")
    if not q or len(q) > 120 or re.search(r"[{}\[\]\"]", q):
        return ""
    return q


def _singular(w: str) -> str:
    return w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w


def clean_terms(raw: list[str], topic: str) -> list[str]:
    """Code-side filtering of Needle's spans before Jev sees them."""
    topic_l = topic.lower()
    topic_words = {_singular(w) for w in re.findall(r"[a-z0-9-]+", topic_l)}
    out: dict[str, str] = {}
    for t in raw:
        t = t.strip(" .,;:()[]\"'")
        words = t.split()
        if not (2 <= len(t) <= 60) or len(words) > 6:
            continue
        if re.search(r"\bet al\b|^\d+$|https?://", t, re.I) or t.lower() in topic_l:
            continue
        if t.count("(") != t.count(")") or t.count("[") != t.count("]"):  # cut-off fragment
            continue
        if t.lower() in _GENERIC_L or _CHROME_TERM.search(t):
            continue
        # "PINNs" for topic "PINN for disease modeling": the topic itself, not a new concept.
        if all(_singular(w) in topic_words for w in re.findall(r"[a-z0-9-]+", t.lower())):
            continue
        out.setdefault(t.lower(), t)
    return list(out.values())


class StubNeedle:
    """Offline stand-in: returns the plan verbatim and regex candidates as terms."""

    calls = 0

    async def draft_queries(self, chunks: list[tuple[str, list[str]]]) -> tuple[list[str], str]:
        return [q for _, c in chunks for q in c], "stub"

    async def extract_terms(self, title: str, text: str, topic: str) -> list[str]:
        return clean_terms(regex_terms(f"{title}\n{text}"), topic)
