"""Web UI: the same pass as the CLI, driven from a browser with the user's own keys.

    uv run research-web                 # http://127.0.0.1:8321
    uv run research-web --env-keys      # fall back to this machine's env keys

Each run gets the keys its browser sent (see config.Keys); they live only in memory for
the run and are never written to run.json. The page watches the knowledge tree grow over
server-sent events and reads the finished artifacts from reports/.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import datetime as dt
import json
import logging
import mimetypes
import re
import time
from collections import deque
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import config as cfg
from . import context
from .jev_client import JEV_PRICE_PER_MTOK
from .main import run, slugify
from .report import PRICES

log = logging.getLogger("research.web")

# The page is ES modules, which browsers refuse unless served as JavaScript. On Windows
# the registry often maps .js to text/plain, and Python's mimetypes reads it.
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/javascript", ".mjs")
mimetypes.add_type("text/css", ".css")

WEB_DIR = Path(__file__).parent / "web"
RUN_ID = re.compile(r"^[a-z0-9-]+--\d{8}-\d{6}(-\d+)?$")
NODE_ID = re.compile(r"^[rqsc]\d+$")
# Settings the browser may change. Paths, URLs of internal services and offline mode
# stay with whoever runs the server.
ALLOWED = {"budgets", "gates", "template", "report_model", "draft_model", "tavily_depth", "search_backends", "tavily_extract_fallback"}
MAX_EVENTS = 400


# --- live runs -------------------------------------------------------------------


class LiveRun:
    """The `ui` object main.run() drives: attach() hands over the Researcher, close() ends it.
    Holds what the event stream needs; the tree itself is read straight off the Researcher."""

    def __init__(self, run_id: str, topic: str, run_dir: Path, settings: cfg.Settings):
        self.id = run_id
        self.topic = topic
        self.dir = run_dir
        self.settings = settings
        self.r = None
        self.judge = None
        self.tavily = None
        self.events: deque[dict] = deque(maxlen=MAX_EVENTS)
        self.seq = 0
        self.t0 = time.monotonic()
        self.status = "running"  # running | done | error | cancelled
        self.error: str | None = None
        self.task: asyncio.Task | None = None
        self.changed = asyncio.Event()

    def attach(self, researcher, judge, tavily) -> None:
        self.r, self.judge, self.tavily = researcher, judge, tavily
        researcher.emit = self.event

    def close(self) -> None:
        self.changed.set()

    def event(self, kind: str, **kw) -> None:
        self.seq += 1
        ev: dict[str, Any] = {"seq": self.seq, "t": round(time.monotonic() - self.t0, 1), "kind": kind}
        if kind == "seeds":
            ev |= {"source": kw["source"], "queries": list(kw["drafts"])}
        elif kind == "gate":
            ev |= {"total": kw["total"], "ran": [{"id": n.id, "label": n.label, "score": n.score} for n in kw["ran"]]}
        elif kind == "frontier":
            ev |= {"size": kw["size"]}
        elif kind == "page":
            n = kw["node"]
            ev |= {"id": n.id, "label": n.label, "depth": n.depth, "url": n.url, "rel": n.data.get("page_relevant"), "via": n.via}
        elif kind == "expand":
            ev |= {"queries": [{"id": q.id, "label": q.label, "via": q.via} for q in kw["queries"]]}
        self.events.append(ev)
        self.changed.set()

    def progress(self) -> dict:
        s = self.settings
        out: dict[str, Any] = {
            "status": self.status,
            "error": self.error,
            "elapsed": round(time.monotonic() - self.t0, 1),
            "max_seconds": s.budgets.max_seconds,
            "stage": self.r.stage if self.r else "starting",
            "stop_reason": self.r.stop_reason if self.r else None,
        }
        if self.r:
            t = self.r.tree
            out["depth"] = [{"spent": a, "budget": b} for a, b in zip(t.spent, s.budgets.pages_per_depth)]
            out["frontier"] = t.frontier_size()
        if self.judge:
            out["jev"] = {"calls": self.judge.calls, "usd": round(self.judge.input_tokens * JEV_PRICE_PER_MTOK / 1e6, 5)}
        if self.tavily:
            out["tavily"] = {"credits": self.tavily.credits, "usd": round(self.tavily.usd, 4)}
        return out


RUNS: dict[str, LiveRun] = {}


def compact(n: dict) -> dict:
    """What the page needs per node. Page bodies and anchors stay on disk."""
    d = n.get("data") or {}
    return {
        "id": n["id"],
        "kind": n["kind"],
        "label": n["label"],
        "parent": n["parent"],
        "via": n["via"],
        "depth": n["depth"],
        "url": n["url"],
        "status": n["status"],
        "score": n["score"],
        "rel": d.get("page_relevant"),
        "sid": d.get("sid"),
        "paths": len(n.get("also_from") or []),
        "ckind": d.get("kind") if n["kind"] == "concept" else None,
        "error": d.get("error"),
    }


def live_nodes(lr: LiveRun) -> dict[str, dict]:
    if not lr.r:
        return {}
    # Shallow views of the live Node objects; asdict() would deep-copy every page body.
    return {
        nid: compact({**vars(node), "also_from": node.also_from, "data": node.data})
        for nid, node in list(lr.r.tree.nodes.items())
    }


def disk_nodes(run_dir: Path) -> dict[str, dict]:
    p = run_dir / "tree.json"
    if not p.exists():
        return {}
    data = json.loads(p.read_text())
    return {nid: compact(n) for nid, n in data["nodes"].items()}


# --- helpers -------------------------------------------------------------------------


def reports_dir(request: Request) -> Path:
    return request.app.state.base_settings.reports_dir


def run_dir_of(request: Request, run_id: str) -> Path | None:
    if not RUN_ID.match(run_id):
        return None
    slug, _, stamp = run_id.partition("--")
    d = reports_dir(request) / slug / stamp
    return d if d.is_dir() else None


def settings_json(s: cfg.Settings) -> dict:
    return json.loads(json.dumps(dataclasses.asdict(s), default=str))


def error(msg: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": msg}, status_code=status)


# --- routes --------------------------------------------------------------------------


async def index(request: Request):
    return FileResponse(WEB_DIR / "index.html")


async def meta(request: Request):
    base: cfg.Settings = request.app.state.base_settings
    env = cfg.Keys.from_env() if request.app.state.env_keys else cfg.Keys()
    presets = {name: settings_json(cfg.apply(base, patch)) for name, patch in cfg.PRESETS.items()}
    return JSONResponse(
        {
            "defaults": settings_json(base),
            "presets": presets,
            "models": sorted(PRICES, key=lambda m: PRICES[m][1]),
            "prices": PRICES,
            "server_keys": {"opencode": bool(env.opencode), "typesafe": bool(env.typesafe), "tavily": bool(env.tavily)},
            "offline": base.offline,
            "year": dt.date.today().year,
        }
    )


async def preview(request: Request):
    """Stage 0 for a topic and template, without running anything."""
    body = await request.json()
    try:
        template = cfg.apply(cfg.Settings(), {"template": body.get("template") or {}}).template
    except (ValueError, TypeError) as e:
        return error(str(e))
    ctx = context.build(body.get("topic") or "your topic", body.get("intent") or None, body.get("focus") or [], template=template)
    return JSONResponse({"context": ctx.render(), "core": ctx.core, "facets": ctx.facets, "state": ctx.as_state()})


async def start_run(request: Request):
    app = request.app
    body = await request.json()
    topic = " ".join(str(body.get("topic") or "").split())
    if not topic:
        return error("topic is required")
    if len(topic) > 300:
        return error("topic is too long")
    patch = body.get("settings") or {}
    if not isinstance(patch, dict) or set(patch) - ALLOWED:
        return error(f"settings may only change: {sorted(ALLOWED)}")
    preset = body.get("preset") or "standard"
    if preset not in cfg.PRESETS:
        return error(f"unknown preset {preset!r}")
    try:
        settings = cfg.apply(cfg.apply(app.state.base_settings, cfg.PRESETS[preset]), patch)
    except (ValueError, TypeError) as e:
        return error(f"bad settings: {e}")
    b = settings.budgets
    if not b.pages_per_depth or any(not isinstance(x, int) or x < 0 or x > 200 for x in b.pages_per_depth) or len(b.pages_per_depth) > 6:
        return error("pages_per_depth: 1 to 6 depths, each 0 to 200 pages")
    if b.max_seconds > 3600 or b.max_jev_calls > 3000:
        return error("max_seconds is capped at 3600 and max_jev_calls at 3000")

    sent = body.get("keys") or {}
    env = cfg.Keys.from_env() if app.state.env_keys else cfg.Keys()
    keys = cfg.Keys(
        opencode=(sent.get("opencode") or "").strip() or env.opencode,
        typesafe=(sent.get("typesafe") or "").strip() or env.typesafe,
        tavily=(sent.get("tavily") or "").strip() or env.tavily,
    )
    write_report = bool(body.get("write_report", True))
    if not settings.offline:
        missing = []
        if not keys.typesafe:
            missing.append("TypeSafe (Jev)")
        if not keys.opencode and (write_report or settings.draft_model):
            missing.append("OpenCode Go")
        if missing:
            return error(f"missing API key: {', '.join(missing)}. Add it on the Keys page.", 401)

    if sum(1 for r in RUNS.values() if r.status == "running") >= app.state.max_runs:
        return error("the server is busy with other runs; try again when one finishes", 429)

    focus = [str(f).strip() for f in body.get("focus") or [] if str(f).strip()][:12]
    intent = (body.get("intent") or "").strip() or None
    slug = slugify(topic)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    run_id = f"{slug}--{stamp}"
    run_dir = settings.reports_dir / slug / stamp
    n = 1
    while run_dir.exists() or run_id in RUNS:
        n += 1
        run_id, run_dir = f"{slug}--{stamp}-{n}", settings.reports_dir / slug / f"{stamp}-{n}"
    run_dir.mkdir(parents=True)

    lr = LiveRun(run_id, topic, run_dir, settings)
    RUNS[run_id] = lr

    async def go():
        try:
            await run(topic, settings, intent, focus, write_report=write_report, ui=lr, keys=keys, run_dir=run_dir)
            lr.status = "done"
        except asyncio.CancelledError:
            lr.status = "cancelled"
        except Exception as e:  # surfaced to the page; the traceback goes to the server log
            log.exception("run %s failed", run_id)
            lr.status, lr.error = "error", str(e)[:500]
        finally:
            lr.changed.set()
            # Everything is on disk now; drop the in-memory tree (page bodies) after the
            # open streams have had time to read the final state.
            await asyncio.sleep(120)
            RUNS.pop(run_id, None)

    lr.task = asyncio.create_task(go())
    return JSONResponse({"id": run_id}, status_code=201)


async def list_runs(request: Request):
    out = []
    root = reports_dir(request)
    for rj in root.glob("*/*/run.json") if root.exists() else []:
        try:
            s = json.loads(rj.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        run_id = f"{rj.parent.parent.name}--{rj.parent.name}"
        if not RUN_ID.match(run_id) or run_id in RUNS and RUNS[run_id].status == "running":
            continue
        out.append(
            {
                "id": run_id,
                "topic": s.get("topic"),
                "status": "error" if s.get("error") else "done",
                "stop_reason": s.get("stop_reason"),
                "cost_usd": s.get("cost_usd_total"),
                "seconds": (s.get("timings") or {}).get("total_s"),
                "sources": ((s.get("tree") or {}).get("by_kind_status") or {}).get("source:scraped", 0),
                "has_report": (rj.parent / "report.md").exists(),
                "mtime": rj.stat().st_mtime,
            }
        )
    for lr in RUNS.values():
        if lr.status == "running":
            out.append({"id": lr.id, "topic": lr.topic, "status": "running", "mtime": time.time()})
    out.sort(key=lambda r: -r["mtime"])
    return JSONResponse(out[:200])


async def get_run(request: Request):
    run_id = request.path_params["run_id"]
    lr = RUNS.get(run_id)
    d = lr.dir if lr else run_dir_of(request, run_id)
    if d is None:
        return error("no such run", 404)
    summary = json.loads((d / "run.json").read_text()) if (d / "run.json").exists() else None
    ctx_file = d / "context_prompt.txt"
    return JSONResponse(
        {
            "id": run_id,
            "topic": lr.topic if lr else (summary or {}).get("topic"),
            "live": bool(lr and lr.status == "running"),
            "progress": lr.progress() if lr else None,
            "events": list(lr.events) if lr else [],
            "summary": summary,
            "context": ctx_file.read_text() if ctx_file.exists() else None,
            "nodes": list((live_nodes(lr) if lr and lr.r else disk_nodes(d)).values()),
            "has_report": (d / "report.md").exists(),
        }
    )


async def stream(request: Request):
    """Server-sent events: progress every tick, new events, and changed tree nodes only."""
    run_id = request.path_params["run_id"]
    lr = RUNS.get(run_id)
    if not lr:
        return error("run is not live", 404)
    last_seq = int(request.query_params.get("after", "0") or 0)

    async def gen():
        nonlocal last_seq
        sent: dict[str, dict] = {}
        first = True
        while True:
            if await request.is_disconnected():
                return
            nodes = live_nodes(lr)
            changed = [n for nid, n in nodes.items() if sent.get(nid) != n]
            sent = nodes
            events = [e for e in lr.events if e["seq"] > last_seq]
            if events:
                last_seq = events[-1]["seq"]
            payload = {"progress": lr.progress(), "events": events, "nodes": changed, "full": first}
            first = False
            yield f"data: {json.dumps(payload, default=str)}\n\n"
            if lr.status != "running":
                yield f"event: end\ndata: {json.dumps({'status': lr.status, 'error': lr.error})}\n\n"
                return
            lr.changed.clear()
            try:
                await asyncio.wait_for(lr.changed.wait(), timeout=0.8)
            except TimeoutError:
                pass
            await asyncio.sleep(0.3)  # coalesce bursts

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


async def cancel_run(request: Request):
    lr = RUNS.get(request.path_params["run_id"])
    if not lr or lr.status != "running" or not lr.task:
        return error("run is not live", 404)
    lr.task.cancel()
    return JSONResponse({"ok": True})


async def get_report(request: Request):
    run_id = request.path_params["run_id"]
    d = RUNS[run_id].dir if run_id in RUNS else run_dir_of(request, run_id)
    if d is None or not (d / "report.md").exists():
        return error("no report", 404)
    return PlainTextResponse((d / "report.md").read_text(), media_type="text/markdown; charset=utf-8")


async def get_node(request: Request):
    """One node in full: decision data, delve picks, and the page text if it was scraped."""
    run_id, nid = request.path_params["run_id"], request.path_params["node_id"]
    if not NODE_ID.match(nid):
        return error("bad node id")
    lr = RUNS.get(run_id)
    d = lr.dir if lr else run_dir_of(request, run_id)
    if d is None:
        return error("no such run", 404)
    node = None
    if lr and lr.r and nid in lr.r.tree.nodes:
        node = dataclasses.asdict(lr.r.tree.nodes[nid])
        node["data"] = {k: v for k, v in node["data"].items() if k not in ("text", "anchors")}
    elif (d / "tree.json").exists():
        node = json.loads((d / "tree.json").read_text())["nodes"].get(nid)
    if node is None:
        return error("no such node", 404)
    page = d / "pages" / f"{nid}.md"
    node["text"] = page.read_text()[:40_000] if page.exists() else None
    return JSONResponse(json.loads(json.dumps(node, default=str)))


def create_app(base_settings: cfg.Settings | None = None, env_keys: bool = False, max_runs: int = 2) -> Starlette:
    app = Starlette(
        routes=[
            Route("/", index),
            Route("/api/meta", meta),
            Route("/api/preview", preview, methods=["POST"]),
            Route("/api/runs", list_runs),
            Route("/api/runs", start_run, methods=["POST"]),
            Route("/api/runs/{run_id}", get_run),
            Route("/api/runs/{run_id}/events", stream),
            Route("/api/runs/{run_id}/cancel", cancel_run, methods=["POST"]),
            Route("/api/runs/{run_id}/report", get_report),
            Route("/api/runs/{run_id}/nodes/{node_id}", get_node),
            Mount("/static", StaticFiles(directory=WEB_DIR), name="static"),
        ]
    )
    app.state.base_settings = base_settings or cfg.load()
    app.state.env_keys = env_keys
    app.state.max_runs = max_runs
    return app


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="research-web", description="Browser UI for super-research. Users bring their own keys.")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8321)
    p.add_argument("--config", type=Path, help="TOML file; the base every browser run starts from")
    p.add_argument("--env-keys", action="store_true", help="use this machine's OPENCODE/TYPESAFE/TAVILY env keys when a browser sends none")
    p.add_argument("--max-runs", type=int, default=2, help="concurrent runs")
    a = p.parse_args(argv)

    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s", datefmt="%H:%M:%S")
    for noisy in ("httpx", "httpx2", "httpcore", "typesafe_sdk"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    app = create_app(cfg.load(config_file=a.config), env_keys=a.env_keys, max_runs=a.max_runs)
    print(f"super-research web UI on http://{a.host}:{a.port}")
    uvicorn.run(app, host=a.host, port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
