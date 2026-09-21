"""Stage 0: expand the raw topic into the research context. Templated, not generated."""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field

# Words that describe the deliverable rather than the subject; stripped for query building
# so "gradient surgery methods and results" searches as "gradient surgery ... survey".
_FILLER = re.compile(r"\b(methods?|results?|and|overview|papers?|research|the|of|latest|recent|approaches|techniques)\b", re.IGNORECASE)
PLAN_CHUNK = 4  # Needle splits short plans reliably; long ones blur together


@dataclass
class ResearchContext:
    topic: str
    core: str  # topic minus deliverable words; the stem of every search
    intent: str
    deliverable: str
    tone: str
    filter: str
    facets: list[str] = field(default_factory=list)  # the search plan Needle splits into queries

    def as_state(self) -> dict:
        """What Jev sees as `research` in every question."""
        return {"topic": self.topic, "intent": self.intent, "deliverable": self.deliverable, "filter": self.filter}

    def render(self) -> str:
        return (
            f"TOPIC: {self.topic}\n"
            f"Intent: {self.intent}\n"
            f"Deliverable context: {self.deliverable}\n"
            f"Tone: {self.tone}. Filter: {self.filter}"
        )

    def needle_prompts(self, n: int) -> list[tuple[str, list[str]]]:
        """(prompt, expected queries) per chunk. Needle grounds every argument in its input,
        so the plan spells out each query and Needle's job is to split it into well-formed
        search calls. It gets only the plan: the full context block confuses it."""
        facets = self.facets[:n]
        chunks = [facets[i : i + PLAN_CHUNK] for i in range(0, len(facets), PLAN_CHUNK)]
        return [("Search for " + ", then search for ".join(c) + ".", c) for c in chunks]


def build(topic: str, intent: str | None = None, focus: list[str] | None = None, today: dt.date | None = None) -> ResearchContext:
    topic = " ".join(topic.split())
    core = " ".join(_FILLER.sub(" ", topic).split()) or topic
    year = (today or dt.date.today()).year
    focus = [f.strip() for f in focus or [] if f.strip()]
    focus_s = f", including {', '.join(focus)}" if focus else ""
    ctx = ResearchContext(
        topic=topic,
        core=core,
        intent=intent
        or f"find papers, benchmarks, and engineering writeups covering {topic}{focus_s}, plus any {year} updates.",
        deliverable="a comparison of methods, empirical results, plus what is missing.",
        tone="research oriented",
        filter="skip product pages, skip tutorials below graduate level, skip SEO listicles.",
    )
    ctx.facets = [
        *(f"{f} {core}" for f in focus),
        core,
        f"{core} survey",
        f"{core} benchmark comparison",
        f"{core} arxiv",
        f"{core} {year}",
        f"{core} limitations",
        f"{core} github implementation",
        f"{core} explained blog",
        f"{core} state of the art",
        f"{core} ablation study",
        f"{core} review {year - 1}",
    ]
    return ctx
