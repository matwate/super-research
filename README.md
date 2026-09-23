# super-research

> This is a vibe-coded toy example for an idea i had

A research agent that spends on one expensive model call per pass. Cheap models do the
routing: a flash LLM drafts the seed queries (~$0.0001), Needle 3 (local) pulls named
concepts out of pages, and Jev (TypeSafe) makes every keep/skip decision. Tavily and SearXNG search. One OpenCode Go LLM call writes the report
from a **knowledge tree** of everything the pass found.

```bash
uv run research "gradient surgery methods and results"
uv run research "gradient surgery" --focus "multi-task learning" --focus PCGrad --preset deep
```

Output: `reports/<slug>/<timestamp>/report.md`, plus the debugging artifacts below.

Needs `TYPESAFE_API_KEY` and `OPENCODE_API_KEY` in the environment; `TAVILY_API_KEY` is
optional but strongly recommended. Needle downloads its engine and weights (~35 MB) from
Hugging Face the first time it runs.

On a terminal you get a live dashboard: current stage, page budget per depth, pages as Jev
scores them, concept expansions, and running Jev/Tavily spend. At the end it prints the
knowledge tree and a cost table. Pass `--plain` for plain log lines (automatic when stderr
isn't a TTY).

## Pipeline

| Stage | Who | What |
| --- | --- | --- |
| 0 | template | topic → intent / deliverable / filter block, plus a facet plan (`survey`, `benchmark comparison`, `arxiv`, …) |
| 1 | flash LLM | `draft_model` (default `glm-5.3-flash`) drafts varied queries in the field's own vocabulary. If the call fails, Needle splits the template plan instead |
| 2 | Jev | one Noul per query: run or skip |
| 3 | Tavily + SearXNG | both backends queried in parallel and merged by URL. Tavily returns page text, so those pages skip scraping |
| 4 | Jev | one Noul per result, bundled per query: scrape or skip |
| 5 | scraper | main text + in-content anchors, capped per page. arXiv is read from `/html/` (full paper), and Tavily extract is the fallback for bot walls and JS-only pages |
| 6 | Jev | page-content Noul (the stop signal), then one Noul per anchor: delve or not; recurse by depth |
| ↻ | Needle + Jev | **concept expansion**: named methods/datasets (Needle + regex) and technical problems ("gradient imbalance", "parameter identifiability"; cue-word extractor) on good pages → a Jev gate per kind → new queries hung off the page that mentioned them → back to 3 |
| 7 | OpenCode Go | one call sees intent, queries, the rendered tree, and page text by relevance, and writes the report |

## The knowledge tree

`tree_manager.py` holds the whole pass. Every query, source, followed link and extracted
concept is a node with the Jev probability that admitted or pruned it:

```
- TOPIC: gradient surgery methods and results
  - query "gradient surgery survey" [0.88] (needle_seed, ran)
    - [S3] Gradient Surgery for Multi-Task Learning <neurips.cc/...> d1 [0.92] content=0.90
      - [S7] PCGrad GitHub repo <github.com/...> d2 [0.81] content=0.77
      - concept: CAGrad (promoted)
        - query "CAGrad gradient surgery" [0.84] (needle_concept, ran)
          - [S12] Conflict-Averse Gradient Descent <arxiv.org/abs/...> d1 ...
```

The frontier is a priority queue over the tree (Jev score × depth decay). The page cache
is keyed by normalized URL (arXiv abs/pdf/html collapse to one key). When several paths
reach the same page, the extra edges are kept (`+N other paths`), and a page reached many
ways is usually central to the topic. The report LLM gets the rendered tree, so it can
explain method lineage and point out thin coverage.

## Tuning budgets

Every budget and gate can be set from a preset, `research.toml`, `--config FILE`, or flags
(see `research.example.toml` for the full list):

```bash
uv run research "..." --preset quick                    # 10+4 pages, ~1 min
uv run research "..." --pages 40,20,10,5                # per-depth page budget; length = max depth
uv run research "..." --gate delve=0.6 --gate relevance=0.45
uv run research "..." --set budgets.expansion_rounds=2 --set concurrency=4
uv run research "..." --model glm-5.3                   # any OpenCode Go model id
uv run research "..." --no-report                       # gather only, no LLM spend
uv run research "..." --set 'search_backends=["searxng"]'       # free search only
uv run research "..." --set tavily_depth=basic          # 1 credit per search instead of 2
uv run research --print-config --preset deep
```

## Artifacts per run

| File | Contents |
| --- | --- |
| `report.md` | the report |
| `tree.md` / `tree.json` | full knowledge tree, including pruned nodes and their scores |
| `jev_verdicts.jsonl` | every Jev call: kind, items, probabilities, tokens, latency |
| `needle_drafts.jsonl` | every Needle call: input, calls, confidence, reasoning |
| `search.jsonl` | every Tavily / SearXNG call: query, URLs, credits, dead engines |
| `context_prompt.txt` | stage-0 block and the Needle plan |
| `report_prompt.txt` | the exact prompt sent to the final LLM |
| `pages/` | extracted text of every scraped page |
| `run.json` | settings, timings, costs, stop reason, delve precision |

`run.json → tree.delve_precision` tracks the success criterion: the share of Jev-approved
delves whose content Jev then scored as relevant (target ≥ 0.7).

## Notes

- Needle 3 grounds every argument in its input and refuses rather than invent. It can't
  brainstorm queries (splitting the template plan, it dropped facets and every run looked
  the same), so a flash LLM drafts the seeds. In a 4-way test all flash models cost about
  $0.0001 per pass; glm-5.3-flash wrote the best set. Use `--set draft_model=""` to go back
  to template seeds. For ambiguous topics, add `--focus` terms or `--intent`.
- Problem phrases exist because niche papers are usually *about a problem*. In testing,
  "PINN for disease modeling" never surfaced a paper on gradient pathology until
  "gradient imbalance", found on a scraped page, became a query.
- Term extraction merges Needle spans (from the most name-dense paragraphs; long inputs make
  it refuse) with regex candidates. Jev's concept gate does the selecting.
- Search backends: `search_backends = ["auto"]` means Tavily + SearXNG when `TAVILY_API_KEY`
  is set, otherwise SearXNG only. Tavily uses `advanced` depth and prefers academic domains
  (arxiv, openreview, NeurIPS, PMLR, ACL, GitHub) without excluding others. That's what makes
  ambiguous topics like "gradient surgery" work. At $0.008/credit (2 credits per advanced
  search) it's about $0.15 for a quick pass and $0.30 for a standard one, the largest cost
  item. `--set tavily_depth=basic` halves it.
- On this SearXNG instance, DuckDuckGo, Brave, Startpage and Google are captcha'd or empty,
  and Google Scholar gets suspended under repeated use. The default engines are
  `bing, google scholar, crossref, arxiv, semantic scholar`.
- Bot walls and JS-only pages go to Tavily extract. If that also fails, they count as failed
  fetches and don't use up page budget.
- Tests run offline with stub Jev/Needle: `uv run pytest`.
