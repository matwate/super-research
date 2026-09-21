from super_research.config import Budgets, Gates, load
from super_research.tree_manager import KnowledgeTree, canonical_fetch_url, normalize_url


def tree(pages=(2, 1)):
    return KnowledgeTree("gradient surgery", Budgets(pages_per_depth=pages), Gates())


def test_normalize_url_dedupes_variants():
    a = normalize_url("https://www.arxiv.org/pdf/2001.06782v3.pdf")
    b = normalize_url("http://arxiv.org/abs/2001.06782/")
    assert a == b == "arxiv.org/abs/2001.06782"
    assert normalize_url("https://x.com/a?utm_source=t&id=1#frag") == "x.com/a?id=1"
    assert normalize_url("https://doi.org/10.48550/arXiv.2001.06782") == "arxiv.org/abs/2001.06782"
    assert normalize_url("https://arxiv.org/html/2001.06782v2") == "arxiv.org/abs/2001.06782"
    assert canonical_fetch_url("https://arxiv.org/pdf/2001.06782v2") == "https://arxiv.org/abs/2001.06782"


def test_url_cache_records_extra_edges():
    t = tree()
    q1 = t.add_query("pcgrad", t.root.id, "needle_seed")
    q2 = t.add_query("gradient surgery", t.root.id, "needle_seed")
    assert t.add_query("PCGrad ", t.root.id, "needle_seed") is None
    s = t.add_source("https://arxiv.org/abs/2001.06782", "PCGrad", q1.id, "search", depth=1)
    assert s is not None
    assert t.add_source("https://arxiv.org/pdf/2001.06782", "dup", q2.id, "search", depth=1) is None
    assert s.also_from == [q2.id]


def test_budgets_per_depth_and_priority():
    t = tree(pages=(2, 1))
    q = t.add_query("q", t.root.id, "needle_seed")
    nodes = []
    for i, score in enumerate([0.6, 0.9, 0.7]):
        n = t.add_source(f"https://e.com/{i}", str(i), q.id, "search", depth=1)
        n.score = score
        t.enqueue(n)
        nodes.append(n)
    deep = t.add_source("https://e.com/deep", "deep", nodes[1].id, "link", depth=2)
    deep.score = 0.99
    t.enqueue(deep)
    too_deep = t.add_source("https://e.com/deeper", "x", deep.id, "link", depth=3)
    t.enqueue(too_deep)
    assert too_deep.status == "over_budget"

    batch = t.pop_batch(10)
    # depth decay: 0.99 * 0.85 < 0.9. Depth-1 budget is 2, so the 0.6 source is dropped.
    assert [n.label for n in batch] == ["1", "deep", "2"]
    assert nodes[0].status == "over_budget"
    assert t.exhausted()


def test_concept_candidates_aggregate_and_skip_queried():
    t = tree()
    q = t.add_query("gradient surgery", t.root.id, "needle_seed")
    p1 = t.add_source("https://a.com", "a", q.id, "search", depth=1)
    p2 = t.add_source("https://b.com", "b", q.id, "search", depth=1)
    for p in (p1, p2):
        t.add_concept("Nash-MTL", p.id)
    t.add_concept("CAGrad", p1.id)
    t.add_query("CAGrad gradient surgery", t.root.id, "needle_concept")
    cands = t.concept_candidates()
    assert [(term, n) for term, n, _ in cands] == [("Nash-MTL", 2)]


def test_render_shows_tree():
    t = tree()
    q = t.add_query("pcgrad", t.root.id, "needle_seed")
    q.status, q.score = "ran", 0.8
    s = t.add_source("https://arxiv.org/abs/1", "PCGrad paper", q.id, "search", depth=1)
    s.status, s.score, s.data["page_relevant"] = "scraped", 0.9, 0.95
    out = t.render()
    assert "TOPIC: gradient surgery" in out and 'query "pcgrad"' in out and "PCGrad paper" in out


def test_config_layers(tmp_path):
    f = tmp_path / "r.toml"
    f.write_text("[budgets]\npages_per_depth = [5, 2]\n[gates]\ndelve = 0.7\n")
    s = load("quick", f, {"budgets": {"page_tokens": 1000}})
    assert s.budgets.pages_per_depth == (5, 2) and s.budgets.max_depth == 2
    assert s.gates.delve == 0.7 and s.budgets.page_tokens == 1000
    assert s.budgets.expansion_queries == 4  # from the quick preset


def test_depth1_cap_defers_instead_of_dropping():
    t = tree(pages=(3,))
    q = t.add_query("q", t.root.id, "needle_seed")
    for i in range(3):
        n = t.add_source(f"https://e.com/{i}", str(i), q.id, "search", depth=1)
        n.score = 0.9 - i / 10
        t.enqueue(n)
    assert [n.label for n in t.pop_batch(10, depth1_cap=2)] == ["0", "1"]
    assert t.frontier_size() == 1
    assert [n.label for n in t.pop_batch(10)] == ["2"]


def test_redirect_to_known_url_is_a_duplicate():
    t = tree()
    q = t.add_query("q", t.root.id, "needle_seed")
    a = t.add_source("https://arxiv.org/abs/1", "a", q.id, "search", depth=1)
    b = t.add_source("https://doi.org/10.1234/x", "b", a.id, "link", depth=2)
    assert t.resolve_redirect(b, "https://arxiv.org/abs/1v2") is a
    assert t.resolve_redirect(a, "https://arxiv.org/abs/1") is None
