"""Fetch a page, extract its main text and the anchors inside that text."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

import httpx
from selectolax.parser import HTMLParser

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36 super-research/0.1"
MAX_BYTES = 4_000_000
CHARS_PER_TOKEN = 4

_STRIP = "script, style, noscript, svg, nav, header, footer, aside, form, iframe, button, [role=navigation], [aria-hidden=true]"
_SKIP_HOSTS = re.compile(
    r"(^|\.)(twitter\.com|x\.com|facebook\.com|linkedin\.com|instagram\.com|t\.co|"
    r"accounts\.google\.com|pinterest\.com|tiktok\.com|youtube\.com|"
    r"scholar\.google\.[a-z.]+|scholar\.archive\.org|search\.crossref\.org)$"
)
_SKIP_PATH = re.compile(
    r"(login|signin|signup|register|logout|/cart|/checkout|privacy|terms|cookie|/tag/|/tags/|/share|"
    r"/tables?/|/figures?/|/metrics$|/citeas|/export-citation|"
    r"\.(pdf|zip|tar|gz|png|jpe?g|gif|svg|mp4|mp3|ppt|pptx|docx?|xlsx?)$)",
    re.I,
)


# Inside a code repo only the root (README) is worth reading; subfolders are noise.
_REPO_SUBPATH = re.compile(r"^/[^/]+/[^/]+/(tree|blob|issues|pulls?|commits?|actions|releases|tags|branches|stargazers|forks|network|wiki|security|graphs|compare|discussions|labels|milestones)(/|$)")
_REPO_HOSTS = ("github.com", "gitlab.com", "huggingface.co")

_BOT_WALL = re.compile(r"^(client challenge|just a moment|attention required|access denied|are you a robot|verify you are human|403 forbidden)", re.I)


@dataclass
class Anchor:
    url: str
    text: str


@dataclass
class Page:
    url: str  # final URL after redirects
    title: str
    text: str
    anchors: list[Anchor] = field(default_factory=list)
    truncated: bool = False
    fetched_by: str = "scraper"


class ScrapeError(Exception):
    pass


def _clean(s: str) -> str:
    return re.sub(r"[ \t\r\f\v]+", " ", re.sub(r"\n\s*\n+", "\n\n", s)).strip()


def extract(html: str, base_url: str, token_budget: int) -> Page:
    tree = HTMLParser(html)
    title_node = tree.css_first("title")
    title = _clean(title_node.text()) if title_node else ""
    if og := tree.css_first('meta[property="og:title"]'):
        title = og.attributes.get("content") or title
    for node in tree.css(_STRIP):
        node.decompose()
    root = tree.css_first("main") or tree.css_first("article") or tree.css_first("[role=main]") or tree.body
    if root is None:
        raise ScrapeError("no body")

    # Text: block-ish elements joined by newlines so paragraphs survive.
    blocks = []
    for node in root.css("h1, h2, h3, h4, p, li, pre, blockquote, td, th, dd, dt, figcaption"):
        t = _clean(node.text(separator=" "))
        if len(t) > 1:
            prefix = "#" * int(node.tag[1]) + " " if node.tag in ("h1", "h2", "h3", "h4") else ""
            blocks.append(prefix + t)
    text = "\n".join(dict.fromkeys(blocks)) or _clean(root.text(separator="\n"))

    limit = token_budget * CHARS_PER_TOKEN
    truncated = len(text) > limit
    text = text[:limit]

    anchors: dict[str, Anchor] = {}
    for a in root.css("a[href]"):
        label = _clean(a.text(separator=" ")) or (a.attributes.get("title") or "")
        if anchor := _anchor(base_url, a.attributes.get("href") or "", label):
            anchors.setdefault(anchor.url, anchor)
    return Page(url=base_url, title=title[:200], text=text, anchors=list(anchors.values()), truncated=truncated)


# Link labels that say nothing about the target: citation-list chrome and generic verbs.
# A reference's real title sits in the surrounding text, not in these anchors.
_CHROME_LABEL = re.compile(
    r"^\[?(google scholar|crossref|cross ref|pubmed|pmc free article|free article|full text|"
    r"full size (table|image|figure)|view (article|table|figure)|article|abstract|pdf|download|"
    r"doi|cas|web of science|scopus|mathscinet|zbmath|isi|reference|ref|link|here|more|"
    r"back to top|cite|citation|export citation|open in a new tab|search in google scholar)\]?$",
    re.I,
)


def _anchor(base_url: str, href: str, label: str) -> Anchor | None:
    """Resolve and filter one link; None for chrome, social, files and self-links."""
    href = href.strip()
    if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
        return None
    url = urljoin(base_url, href).split("#")[0]
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return None
    host = parts.netloc.lower().removeprefix("www.")
    if _SKIP_HOSTS.search(host) or _SKIP_PATH.search(parts.path):
        return None
    if host in _REPO_HOSTS and _REPO_SUBPATH.match(parts.path):
        return None
    # Same-site links with tiny labels are almost always navigation chrome.
    if host == urlsplit(base_url).netloc.lower().removeprefix("www.") and len(label) < 4:
        return None
    if url.rstrip("/") == base_url.rstrip("/") or not label or _CHROME_LABEL.match(label.strip()):
        return None
    return Anchor(url=url, text=label[:160])


_MD_LINK = re.compile(r"(?<!!)\[([^\]]{1,200})\]\((https?://[^)\s]+)\)")


def page_from_markdown(url: str, title: str, md: str, token_budget: int) -> Page:
    """A page from text a search/extract API already fetched (e.g. Tavily raw content).
    Markdown links become anchors so delving works the same as for scraped HTML."""
    anchors: dict[str, Anchor] = {}
    for label, href in _MD_LINK.findall(md):
        if anchor := _anchor(url, href, _clean(label)):
            anchors.setdefault(anchor.url, anchor)
    text = _clean(_MD_LINK.sub(r"\1", md))
    limit = token_budget * CHARS_PER_TOKEN
    return Page(url=url, title=title[:200], text=text[:limit], anchors=list(anchors.values()), truncated=len(text) > limit)


async def fetch(client: httpx.AsyncClient, url: str, token_budget: int) -> Page:
    try:
        async with client.stream("GET", url, follow_redirects=True, timeout=20.0) as resp:
            if resp.status_code >= 400:
                raise ScrapeError(f"HTTP {resp.status_code}")
            ctype = resp.headers.get("content-type", "")
            if "html" not in ctype and "xml" not in ctype:
                raise ScrapeError(f"not html: {ctype or 'unknown'}")
            body = bytearray()
            async for chunk in resp.aiter_bytes():
                body.extend(chunk)
                if len(body) > MAX_BYTES:
                    break
            html = body.decode(resp.encoding or "utf-8", errors="replace")
            final = str(resp.url)
    except httpx.HTTPError as e:
        raise ScrapeError(f"{type(e).__name__}: {e}") from e
    page = extract(html, final, token_budget)
    if _BOT_WALL.search(page.title):
        raise ScrapeError(f"bot wall: {page.title}")
    if len(page.text) < 200:
        raise ScrapeError("too little text (blocked, JS-only, or empty)")
    return page


def worth_extracting(err: ScrapeError) -> bool:
    """Failures an extract API can get past: bot walls, auth/rate blocks, JS-only pages.
    Not 404s, non-HTML files or network errors."""
    msg = str(err)
    return msg.startswith(("bot wall", "HTTP 401", "HTTP 403", "HTTP 429", "too little text"))


def new_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5"},
        limits=httpx.Limits(max_connections=16),
        http2=False,
    )
