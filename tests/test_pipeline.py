"""Offline end-to-end: stub Jev/Needle, fake search and fetch, no network."""

import json

from super_research import config, context
from super_research.jev_client import StubJudge
from super_research.main import Researcher, build_overrides, main, run
from super_research.needle_client import StubNeedle, clean_terms, problem_phrases
from super_research.drafter import parse as parse_draft
from super_research.report import assemble, strip_reasoning
from super_research.scraper import extract
from super_research.searx import SearchResult

HTML = """<html><head><title>Gradient Surgery for Multi-Task Learning</title></head><body>
<nav><a href="/home">Home</a></nav>
<main><h1>Gradient Surgery</h1><p>PCGrad projects conflicting task gradients. Compared to MGDA and GradNorm
on NYUv2 it improves multi-task learning results.</p>
<p>See <a href="/cagrad">CAGrad conflict-averse gradient surgery</a> and <a href="https://twitter.com/x">tweet</a>
and <a href="https://github.com/a/b/tree/main/x">examples folder</a>.</p>
</main><footer><a href="/privacy">Privacy</a></footer></body></html>"""


def test_extract_main_text_and_anchors():
    page = extract(HTML, "https://ex.org/pcgrad", token_budget=1000)
    assert page.title == "Gradient Surgery for Multi-Task Learning"
    assert "PCGrad projects" in page.text and "Home" not in page.text
    assert [a.url for a in page.anchors] == ["https://ex.org/cagrad"]


def test_citation_chrome_links_are_dropped():
    html = """<html><body><main><p>Prior work on SIR models is extensive and relevant here.</p><ul>
    <li>Raissi et al. <a href="https://scholar.google.com/scholar_lookup?title=PINN">Google Scholar</a>
    <a href="https://doi.org/10.1038/x">CrossRef</a> <a href="https://doi.org/10.1371/y">DOI</a>
    <a href="https://www.nature.com/articles/s41598/tables/9">Full size table</a>
    <a href="https://doi.org/10.3934/bdia.2025012">10.3934/bdia.2025012</a>
    <a href="https://github.com/a/pinn">this https URL</a></li></ul></main></body></html>"""
    page = extract(html, "https://ex.org/paper", token_budget=1000)
    assert [a.url for a in page.anchors] == ["https://doi.org/10.3934/bdia.2025012", "https://github.com/a/pinn"]


def test_clean_terms():
    raw = ["PCGrad", "Yu et al., 2020", "gradient surgery", "Multi-task learning often suffers from conflicting gradients", "PCGrad", "NYUv2"]
    assert clean_terms(raw, "gradient surgery methods") == ["PCGrad", "NYUv2"]


def test_problem_phrases():
    text = (
        "PINNs suffer from gradient imbalance between loss terms. The gradient imbalance grows with "
        "stiffness of the ODE. Another major limitation is parameter identifiability, and preventing "
        "overfitting under extreme data scarcity."
    )
    got = problem_phrases(text, "PINN for disease modeling")
    assert got[0] == "gradient imbalance"
    assert {"stiffness", "parameter identifiability", "data scarcity", "overfitting"} <= set(got)
    assert not any(w in " ".join(got) for w in ("major", "extreme", "preventing"))


def test_clean_terms_drops_chrome_and_topic_plurals():
    raw = ["PINNs", "SEIR", "README.md", "README", "PMC", "ORCID", "bib7", "machine learning (ML", "ODE-PINN", "DevOps", "pinn"]
    assert clean_terms(raw, "PINN for disease modeling") == ["SEIR", "ODE-PINN"]


def fake_web():
    pages = {
        "https://ex.org/pcgrad": HTML,
        "https://ex.org/cagrad": HTML.replace("Gradient Surgery for", "CAGrad: conflict-averse gradient surgery for").replace('href="/cagrad"', 'href="/famo"'),
        "https://ex.org/famo": HTML.replace("Gradient Surgery for", "FAMO fast adaptive gradient surgery"),
    }

    async def search_fn(q):
        return [
            SearchResult("https://ex.org/pcgrad", "Gradient surgery PCGrad", "gradient surgery multi-task", ["bing"]),
            SearchResult("https://shop.example.com/x", "Buy scalpels", "surgical tools", ["bing"]),
        ]

    async def fetch_fn(url):
        if url not in pages:
            raise RuntimeError("404")
        return extract(pages[url], url, 1000)

    return search_fn, fetch_fn


async def test_researcher_builds_tree(tmp_path):
    s = config.load("standard", None, {"budgets": {"pages_per_depth": [3, 2, 1], "seed_queries": 3}})
    ctx = context.build("gradient surgery")
    search_fn, fetch_fn = fake_web()
    r = Researcher(ctx, s, tmp_path, StubJudge(), StubNeedle(), search_fn, fetch_fn)
    await r.gather()
    scraped = {n.url for n in r.tree.of("source", "scraped")}
    assert "https://ex.org/pcgrad" in scraped
    assert "https://ex.org/cagrad" in scraped  # delved at depth 2
    assert "https://ex.org/famo" in scraped  # depth 3
    assert all(n.url != "https://shop.example.com/x" for n in r.tree.of("source", "scraped"))
    # concept expansion ran and hung queries off concept nodes
    assert r.tree.of("query") and any(q.via == "needle_concept" for q in r.tree.of("query"))
    msg, n = assemble(ctx, r.tree, 20_000)
    assert n >= 1 and "[S1]" in msg and "KNOWLEDGE TREE" in msg


async def test_run_offline_writes_artifacts(tmp_path, monkeypatch):
    search_fn, fetch_fn = fake_web()
    monkeypatch.setattr("super_research.searx.search", lambda *a, **k: _wrap(search_fn, a[2]))
    monkeypatch.setattr("super_research.scraper.fetch", lambda client, url, budget: fetch_fn(url))
    s = config.load("quick", None, {"offline": True, "reports_dir": str(tmp_path)})
    d = await run("gradient surgery", s, None, [])
    for f in ("report.md", "tree.json", "tree.md", "run.json", "context_prompt.txt", "jev_verdicts.jsonl"):
        assert (d / f).exists(), f
    summary = json.loads((d / "run.json").read_text())
    assert summary["tree"]["pages_per_depth_spent"][0] >= 1


async def _wrap(search_fn, q):
    return await search_fn(q), []


def test_cli_overrides():
    import argparse

    ns = argparse.Namespace(
        pages="10,5", seed_queries=4, expansion_rounds=None, max_seconds=None, max_jev_calls=None,
        report_tokens=None, page_tokens=None, gate=["delve=0.6"], model="glm-5.3", searx=None,
        engines="bing,arxiv", offline=False, set=["budgets.delve_per_page=3", "concurrency=4"],
    )
    o = build_overrides(ns)
    s = config.load("standard", None, o)
    assert s.budgets.pages_per_depth == (10, 5) and s.gates.delve == 0.6
    assert s.budgets.delve_per_page == 3 and s.concurrency == 4 and s.searx_engines == ("bing", "arxiv")


def test_print_config(capsys):
    main(["--print-config", "--pages", "4,2"])
    assert json.loads(capsys.readouterr().out)["budgets"]["pages_per_depth"] == [4, 2]


def test_strip_reasoning():
    assert strip_reasoning("<think>plan the report\nsources...</think>\n\n# Title\nbody") == "# Title\nbody"
    assert strip_reasoning("# Title\nno reasoning") == "# Title\nno reasoning"
    assert strip_reasoning("<think>never finished") == ""


def test_parse_draft():
    text = '<think>hmm</think>Sure:\n```json\n{"queries": ["PINN SEIR", "PINN SEIR", " stiff ODE PINN ", 3, ""]}\n```'
    assert parse_draft(text, 10) == ["PINN SEIR", "stiff ODE PINN"]
    assert parse_draft("no json here", 10) == []


async def test_seed_queries_use_drafter_then_fallback(tmp_path):
    from super_research.drafter import Draft

    s = config.load("standard", None, {"budgets": {"seed_queries": 4}})
    ctx = context.build("gradient surgery")
    search_fn, fetch_fn = fake_web()

    async def good(c, n):
        return Draft(["PCGrad multi-task conflicting gradients", "gradient surgery survey"], "glm-5.3-flash", 100, 50, 0.0001)

    r = Researcher(ctx, s, tmp_path, StubJudge(), StubNeedle(), search_fn, fetch_fn, good)
    await r.seed_queries()
    assert {q.label for q in r.tree.of("query")} == {"PCGrad multi-task conflicting gradients", "gradient surgery survey"}
    assert all(q.via == "llm_seed" for q in r.tree.of("query"))

    async def broken(c, n):
        raise RuntimeError("503")

    r = Researcher(ctx, s, tmp_path, StubJudge(), StubNeedle(), search_fn, fetch_fn, broken)
    await r.seed_queries()
    assert [q.label for q in r.tree.of("query")] == ctx.facets[:4]
    assert all(q.via == "needle_seed" for q in r.tree.of("query"))
