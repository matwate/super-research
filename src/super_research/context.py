"""Stage 0: expand the raw topic into the research context. Templated, not generated."""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field

from .config import Template

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


_PLACEHOLDER = re.compile(r"\{(topic|core|year|last_year|focus)\}")


def fill(text: str, values: dict[str, str]) -> str:
    """Substitutes the known placeholders only, so a stray brace in a user template is kept."""
    return " ".join(_PLACEHOLDER.sub(lambda m: values[m.group(1)], text).split())


def build(
    topic: str,
    intent: str | None = None,
    focus: list[str] | None = None,
    today: dt.date | None = None,
    template: Template | None = None,
) -> ResearchContext:
    t = template or Template()
    topic = " ".join(topic.split())
    core = " ".join(_FILLER.sub(" ", topic).split()) or topic
    year = (today or dt.date.today()).year
    focus = [f.strip() for f in focus or [] if f.strip()]
    values = {
        "topic": topic,
        "core": core,
        "year": str(year),
        "last_year": str(year - 1),
        "focus": f", including {', '.join(focus)}" if focus else "",
    }
    ctx = ResearchContext(
        topic=topic,
        core=core,
        intent=intent or fill(t.intent, values),
        deliverable=fill(t.deliverable, values),
        tone=fill(t.tone, values),
        filter=fill(t.filter, values),
    )
    facets = [f"{f} {core}" for f in focus] + [fill(f, values) for f in t.facets]
    ctx.facets = list(dict.fromkeys(f for f in facets if f))
    return ctx
