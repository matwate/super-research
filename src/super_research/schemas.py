"""Grammar schemas for Needle and the node type of the knowledge tree."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

# --- Needle grammar schemas -------------------------------------------------------
# Needle compiles these into a byte-level grammar, so every call it emits parses.
# Needle grounds arguments in its input: it splits and copies, it does not invent.
# Both tools are therefore shaped as "pick spans out of the text I give you".

SEARCH_TOOL: dict[str, Any] = {
    "name": "search_web",
    "description": "Search the web for one keyword query and return result links.",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "keyword search query"},
        },
        "required": ["query"],
    },
}

TERMS_TOOL: dict[str, Any] = {
    "name": "record_terms",
    "description": "Record the named methods, algorithms, models, datasets and benchmarks mentioned in the text.",
    "parameters": {
        "type": "object",
        "properties": {
            "terms": {
                "type": "array",
                "items": {"type": "string"},
                "description": "named methods, algorithms, models, datasets or benchmarks",
            },
        },
        "required": ["terms"],
    },
}

# --- Knowledge tree -------------------------------------------------------------

NodeKind = Literal["root", "query", "source", "concept"]
# How a node came to exist; the edge label from its parent.
Via = Literal["topic", "needle_seed", "needle_concept", "search", "link", "extract"]
Status = Literal[
    "pending",  # created, no decision yet
    "skipped",  # a Jev gate said no
    "ran",  # query executed
    "queued",  # source approved, waiting in the frontier
    "scraped",
    "failed",  # fetch/extract error
    "over_budget",  # approved but the depth budget ran out
    "promoted",  # concept turned into a query
]


@dataclass
class Node:
    id: str
    kind: NodeKind
    label: str  # query text, page title, concept name, or topic
    parent: str | None
    via: Via
    depth: int = 0  # hop depth for sources: 1 = from search, 2+ = delved links
    url: str | None = None
    status: Status = "pending"
    score: float | None = None  # the Jev probability that admitted (or rejected) this node
    reason: str = ""  # why: anchor text, snippet, gate name
    children: list[str] = field(default_factory=list)
    # Other nodes that also pointed at this URL (the tree is really a DAG; in-degree
    # is a useful signal that a source is central to the topic).
    also_from: list[str] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
