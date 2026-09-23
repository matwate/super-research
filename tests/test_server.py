"""Web server: runs the real pass (offline stubs, fake web) and serves the tree."""

import json
import time

import pytest
from starlette.testclient import TestClient

from super_research import config, context
from super_research.server import RUNS, create_app

from test_pipeline import _wrap, fake_web


@pytest.fixture
def client(tmp_path, monkeypatch):
    search_fn, fetch_fn = fake_web()
    monkeypatch.setattr("super_research.searx.search", lambda *a, **k: _wrap(search_fn, a[2]))
    monkeypatch.setattr("super_research.scraper.fetch", lambda client, url, budget: fetch_fn(url))
    RUNS.clear()
    base = config.load("standard", None, {"offline": True, "reports_dir": str(tmp_path)})
    with TestClient(create_app(base)) as c:
        yield c


def wait_done(client, run_id, timeout=20):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        info = client.get(f"/api/runs/{run_id}").json()
        if not info["live"] and info["summary"]:
            return info
        time.sleep(0.1)
    raise AssertionError("run did not finish")


def test_template_changes_stage_zero():
    t = config.Template(intent="map {topic} for {year}", tone="casual", facets=("{core} code", "{core} {last_year}", "{nope} kept"))
    ctx = context.build("gradient surgery methods", focus=["PCGrad"], template=t)
    assert ctx.intent.startswith("map gradient surgery methods for ")
    assert ctx.tone == "casual"
    assert ctx.facets[0] == "PCGrad gradient surgery" and ctx.facets[1] == "gradient surgery code"
    assert ctx.facets[-1] == "{nope} kept"  # unknown braces are left as typed


def test_meta_and_preview(client):
    meta = client.get("/api/meta").json()
    assert set(meta["presets"]) == {"quick", "standard", "deep"}
    assert meta["defaults"]["template"]["facets"][0] == "{core}"
    p = client.post("/api/preview", json={"topic": "gradient surgery", "template": {"facets": ["{core} survey", "{core} code"]}}).json()
    assert p["facets"] == ["gradient surgery survey", "gradient surgery code"]
    assert "TOPIC: gradient surgery" in p["context"]


def test_run_streams_tree_and_writes_artifacts(client, tmp_path):
    body = {
        "topic": "gradient surgery",
        "preset": "quick",
        "settings": {
            "budgets": {"pages_per_depth": [3, 2, 1], "seed_queries": 3},
            "template": {"tone": "terse", "facets": ["{core}", "{core} survey", "{core} code"]},
        },
        "keys": {"opencode": "sk-browser-secret", "typesafe": "ts-browser-secret"},
    }
    r = client.post("/api/runs", json=body)
    assert r.status_code == 201, r.text
    run_id = r.json()["id"]

    info = wait_done(client, run_id)
    kinds = {n["kind"] for n in info["nodes"]}
    assert {"root", "query", "source"} <= kinds
    scraped = [n for n in info["nodes"] if n["kind"] == "source" and n["status"] == "scraped"]
    assert scraped and any(n["sid"] for n in scraped)
    assert info["summary"]["settings"]["template"]["tone"] == "terse"
    assert "terse" in info["context"]

    report = client.get(f"/api/runs/{run_id}/report")
    assert report.status_code == 200 and "offline dry run" in report.text
    node = client.get(f"/api/runs/{run_id}/nodes/{scraped[0]['id']}").json()
    assert node["text"] and "PCGrad" in node["text"]

    # Keys the browser sent never land on disk.
    run_dir = tmp_path / "gradient-surgery"
    for f in run_dir.rglob("*"):
        if f.is_file():
            assert "browser-secret" not in f.read_text(errors="ignore"), f

    listed = client.get("/api/runs").json()
    assert listed[0]["id"] == run_id and listed[0]["has_report"]


def test_keys_required_online(tmp_path):
    base = config.load("standard", None, {"reports_dir": str(tmp_path)})
    with TestClient(create_app(base)) as c:
        r = c.post("/api/runs", json={"topic": "x", "keys": {}})
        assert r.status_code == 401 and "TypeSafe" in r.json()["error"]


def test_rejects_server_side_settings(client):
    r = client.post("/api/runs", json={"topic": "x", "settings": {"reports_dir": "/tmp/elsewhere"}})
    assert r.status_code == 400
    r = client.post("/api/runs", json={"topic": "x", "settings": {"budgets": {"pages_per_depth": [5000]}}})
    assert r.status_code == 400
    assert client.get("/api/runs/..%2F..%2Fetc").status_code == 404
    assert client.get("/api/runs/a--20260101-000000/nodes/..%2Fx").status_code in (400, 404)


def test_stream_sends_nodes(client):
    run_id = client.post("/api/runs", json={"topic": "gradient surgery", "preset": "quick", "settings": {"budgets": {"pages_per_depth": [2]}}}).json()["id"]
    with client.stream("GET", f"/api/runs/{run_id}/events") as s:
        payloads = []
        for line in s.iter_lines():
            if line.startswith("data: ") and '"progress"' in line:
                payloads.append(json.loads(line[6:]))
            if line.startswith("event: end"):
                break
    assert payloads[0]["full"] is True
    assert any(n["kind"] == "root" for p in payloads for n in p["nodes"])
    wait_done(client, run_id)
