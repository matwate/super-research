"""The knowledge tree: every query, source, link and concept the pass touches, with the
decision that admitted or pruned it.

The tree is the run's single source of truth. The frontier, URL cache and budgets are
views over it, the final report is written from it, and tree.json / tree.md are the
debugging record of why each page was (or was not) read.

    root (topic)
    ├── query        via needle_seed / needle_concept, scored by Jev's query gate
    │   └── source   via search, scored by Jev's relevance gate
    │       ├── source   via link (delve), depth + 1
    │       └── concept  via extract (Needle), promoted into a new query
    │           └── query ...
"""

from __future__ import annotations

import heapq
import itertools
import json
import re
from collections import Counter
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .config import Budgets, Gates
from .schemas import Node, NodeKind, Via

_TRACKING = re.compile(r"^(utm_|fbclid|gclid|ref$|ref_|source$|mc_)")
_ARXIV_PDF = re.compile(r"^/(?:pdf|html)/([^/]+?)(v\d+)?(\.pdf)?/?$")
_ARXIV_DOI = re.compile(r"^/10\.48550/arxiv\.(.+)$", re.I)


def normalize_url(url: str) -> str:
    """Cache key for a URL: scheme-less, no fragment/tracking params, arXiv pdf -> abs."""
    parts = urlsplit(url.strip())
    host = parts.netloc.lower().removeprefix("www.")
    path = parts.path or "/"
    if host in ("doi.org", "dx.doi.org") and (m := _ARXIV_DOI.match(path)):
        host, path = "arxiv.org", f"/abs/{m.group(1)}"
    if host.endswith("arxiv.org"):
        if m := _ARXIV_PDF.match(path):
            path = f"/abs/{m.group(1)}"
        path = re.sub(r"(/abs/[^/]+?)v\d+$", r"\1", path)
    if len(path) > 1:
        path = path.rstrip("/")
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query) if not _TRACKING.match(k)])
    return urlunsplit(("", host, path, query, "")).removeprefix("//")


def canonical_fetch_url(url: str) -> str:
    """URL to actually fetch: arXiv PDFs become abstract pages (PDFs are out of scope)."""
    parts = urlsplit(url)
    if parts.netloc.lower().endswith("arxiv.org") and (m := _ARXIV_PDF.match(parts.path)):
        return f"https://arxiv.org/abs/{m.group(1)}"
    return urlunsplit((parts.scheme or "https", parts.netloc, parts.path, parts.query, ""))


class KnowledgeTree:
    def __init__(self, topic: str, budgets: Budgets, gates: Gates):
        self.budgets = budgets
        self.gates = gates
        self.nodes: dict[str, Node] = {}
        self._ids = itertools.count()
        self._url_index: dict[str, str] = {}
        self._queries: dict[str, str] = {}
        self._frontier: list[tuple[float, int, str]] = []
        self._seq = itertools.count()
        # Pages scraped (or in flight) per depth, indexed depth - 1.
        self.spent = [0] * budgets.max_depth
        self.root = self._add("root", topic, None, "topic")

    # --- construction ----------------------------------------------------------

    def _add(self, kind: NodeKind, label: str, parent: str | None, via: Via, **kw) -> Node:
        node = Node(id=f"{kind[0]}{next(self._ids)}", kind=kind, label=label, parent=parent, via=via, **kw)
        self.nodes[node.id] = node
        if parent:
            self.nodes[parent].children.append(node.id)
        return node

    def add_query(self, text: str, parent: str, via: Via) -> Node | None:
        """A candidate query. Returns None if an equivalent query already exists."""
        key = " ".join(text.lower().split())
        if not key or key in self._queries:
            return None
        node = self._add("query", text.strip(), parent, via)
        self._queries[key] = node.id
        return node

    def add_source(
        self,
        url: str,
        title: str,
        parent: str,
        via: Via,
        depth: int,
        reason: str = "",
        **data,
    ) -> Node | None:
        """A candidate page. If the URL is already in the tree, record the extra edge and
        return None: the page cache is keyed by normalized URL, so nothing is revisited."""
        key = normalize_url(url)
        if existing := self._url_index.get(key):
            if parent not in self.nodes[existing].also_from and parent != self.nodes[existing].parent:
                self.nodes[existing].also_from.append(parent)
            return None
        node = self._add("source", title or url, parent, via, depth=depth, url=canonical_fetch_url(url), reason=reason, data=data)
        self._url_index[key] = node.id
        return node

    def resolve_redirect(self, node: Node, final_url: str) -> Node | None:
        """After fetching: if the page redirected to a URL already in the tree, return
        that node (the fetch was a duplicate). Otherwise index the final URL too."""
        key = normalize_url(final_url)
        existing = self._url_index.get(key)
        if existing and existing != node.id:
            if node.parent not in self.nodes[existing].also_from:
                self.nodes[existing].also_from.append(node.parent)
            return self.nodes[existing]
        self._url_index[key] = node.id
        return None

    def add_concept(self, term: str, parent: str) -> Node:
        return self._add("concept", term, parent, "extract")

    def known_url(self, url: str) -> bool:
        return normalize_url(url) in self._url_index

    # --- frontier & budgets ----------------------------------------------------

    def priority(self, node: Node) -> float:
        return (node.score or 0.0) * self.gates.depth_decay ** (node.depth - 1)

    def enqueue(self, node: Node) -> None:
        if node.depth > self.budgets.max_depth:
            node.status = "over_budget"
            return
        node.status = "queued"
        heapq.heappush(self._frontier, (-self.priority(node), next(self._seq), node.id))

    def has_room(self, depth: int) -> bool:
        return 1 <= depth <= self.budgets.max_depth and self.spent[depth - 1] < self.budgets.pages_per_depth[depth - 1]

    def pop_batch(self, n: int, depth1_cap: int | None = None) -> list[Node]:
        """Highest-priority queued sources that fit their depth budget. Budget is
        reserved on pop so concurrent scrapes cannot overshoot. `depth1_cap` holds part
        of the depth-1 budget back (for concept expansion); capped nodes stay queued."""
        batch: list[Node] = []
        deferred = []
        while self._frontier and len(batch) < n:
            item = heapq.heappop(self._frontier)
            node = self.nodes[item[2]]
            if not self.has_room(node.depth):
                node.status = "over_budget"
                continue
            if node.depth == 1 and depth1_cap is not None and self.spent[0] >= depth1_cap:
                deferred.append(item)
                continue
            self.spent[node.depth - 1] += 1
            batch.append(node)
        for item in deferred:
            heapq.heappush(self._frontier, item)
        return batch

    def frontier_size(self) -> int:
        return len(self._frontier)

    def depth1_room(self) -> int:
        return self.budgets.pages_per_depth[0] - self.spent[0]

    def exhausted(self) -> bool:
        return all(not self.has_room(d) for d in range(1, self.budgets.max_depth + 1))

    # --- views ----------------------------------------------------------------

    def of(self, kind: NodeKind, status: str | None = None) -> list[Node]:
        return [n for n in self.nodes.values() if n.kind == kind and (status is None or n.status == status)]

    def ancestry(self, node: Node) -> list[Node]:
        chain = []
        while node.parent:
            node = self.nodes[node.parent]
            chain.append(node)
        return chain[::-1]

    def origin_query(self, node: Node) -> Node | None:
        return next((n for n in reversed(self.ancestry(node)) if n.kind == "query"), None)

    def concept_candidates(self) -> list[tuple[str, int, list[str]]]:
        """Pending concepts aggregated across pages: (term, mention count, node ids),
        most-mentioned first. Terms that already have a query are dropped."""
        groups: dict[str, list[str]] = {}
        names: dict[str, Counter] = {}
        for n in self.of("concept", "pending"):
            key = " ".join(n.label.lower().split())
            groups.setdefault(key, []).append(n.id)
            names.setdefault(key, Counter())[n.label] += 1
        out = [
            (names[k].most_common(1)[0][0], len(ids), ids)
            for k, ids in groups.items()
            if not any(k in q for q in self._queries)
        ]
        return sorted(out, key=lambda t: -t[1])

    def scraped_sources(self) -> list[Node]:
        """Scraped pages ordered by how useful Jev judged their content."""
        pages = self.of("source", "scraped")
        return sorted(pages, key=lambda n: -(n.data.get("page_relevant") or 0.0))

    def stats(self) -> dict:
        by = Counter((n.kind, n.status) for n in self.nodes.values())
        delved = [n for n in self.of("source", "scraped") if n.via == "link"]
        good = [n for n in delved if (n.data.get("page_relevant") or 0) >= 0.5]
        return {
            "nodes": len(self.nodes),
            "by_kind_status": {f"{k}:{s}": c for (k, s), c in sorted(by.items())},
            "pages_per_depth_spent": self.spent,
            "pages_per_depth_budget": list(self.budgets.pages_per_depth),
            "frontier_left": len(self._frontier),
            # Success criterion: >= 70% of Jev-approved delves hold relevant content.
            "delve_precision": round(len(good) / len(delved), 3) if delved else None,
            "delved_pages": len(delved),
        }

    # --- rendering ---------------------------------------------------------------

    def render(self, include_pruned: bool = False, max_children: int = 40) -> str:
        """Indented outline of the tree. The report LLM and humans both read this."""
        lines: list[str] = []

        def fmt(n: Node) -> str:
            score = f" [{n.score:.2f}]" if n.score is not None else ""
            if n.kind == "root":
                return f"TOPIC: {n.label}"
            if n.kind == "query":
                return f'query "{n.label}"{score} ({n.via}, {n.status})'
            if n.kind == "concept":
                return f"concept: {n.label} ({n.status})"
            tag = f"S{n.data['sid']}" if "sid" in n.data else n.status
            rel = n.data.get("page_relevant")
            rel_s = f" content={rel:.2f}" if rel is not None else ""
            extra = f" +{len(n.also_from)} other paths" if n.also_from else ""
            return f"[{tag}] {n.label[:90]} <{n.url}> d{n.depth}{score}{rel_s}{extra}"

        def keep(n: Node) -> bool:
            if include_pruned:
                return True
            if n.kind == "source":
                return n.status == "scraped"
            if n.kind == "query":
                return n.status == "ran"
            if n.kind == "concept":
                return n.status == "promoted"
            return True

        def walk(nid: str, indent: int) -> None:
            n = self.nodes[nid]
            lines.append("  " * indent + "- " + fmt(n))
            kids = [self.nodes[c] for c in n.children if keep(self.nodes[c])]
            kids.sort(key=lambda k: -(k.score or 0))
            for k in kids[:max_children]:
                walk(k.id, indent + 1)
            if len(kids) > max_children:
                lines.append("  " * (indent + 1) + f"- ... {len(kids) - max_children} more")

        walk(self.root.id, 0)
        return "\n".join(lines)

    def save(self, path: Path) -> None:
        """tree.json without page bodies (those are written to pages/ separately)."""
        nodes = {}
        for k, v in self.nodes.items():
            d = v.to_dict()
            d["data"] = {dk: dv for dk, dv in d["data"].items() if dk not in ("text", "anchors")}
            nodes[k] = d
        path.write_text(
            json.dumps(
                {"root": self.root.id, "stats": self.stats(), "nodes": nodes},
                indent=1,
                default=str,
            )
        )
