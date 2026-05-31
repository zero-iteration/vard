# VARD

**A fast, local retrieval layer that gives AI coding agents the right code for a task — including the code coupled through *shared data* (caches, DBs, queues) that grep, embeddings, and call-graphs all miss.**

<!-- demo: record a short clip per docs/README.md, save it as docs/usage.gif, and uncomment ↓ -->
<!-- <p align="center"><img src="docs/usage.gif" alt="VARD usage demo" width="720"></p> -->


VARD finds context. It does not write code. It is a retrieval layer that runs *before* the model, so a coding agent (or you) starts from the right files and the hidden couplings instead of grepping for them. Local and key-optional by default.

## The idea

When you ask an AI to fix a bug, the hardest context is often the *invisible* file. A background worker writes a value to a cache key; a web handler reads it later. Change one and the other breaks — yet there is **no call, no import, no shared text, and no semantic similarity** between them. Every text/embedding/call-graph retriever is blind to this. VARD makes the link explicit.

```text
$ vard context "the status page shows stale data"

## Directly relevant
- monitor.py:49-58              get_status           ← what grep / embeddings already find

## Coupled through shared data (grep/embeddings miss these)
- tasks/maintenance.py:84-117   refresh_queries      ⮂ writes redis:status that get_status reads
                                                       ← the actual root cause
```

In one real repo (redash) that root-cause writer ranked **#450 by grep and #180 by a state-of-the-art embedding model** — effectively unfindable. VARD links it directly through the shared `redis:status` key.

## Results

The metric VARD is built for is **file localization**: given an issue, are the files that actually need changing in the top-k?

**On SWE-bench Verified** (109 real GitHub issues; gold = the files the accepted patch edits), free local embeddings, ~1s/query, no LLM in the loop:

| top-k | found the right file (any@k) | recall@k |
|------:|:----------------------------:|:--------:|
|   1   | 0.35 | 0.32 |
|   5   | 0.67 | 0.64 |
|  10   | **0.75** | **0.74** |
|  20   | 0.84 | 0.83 |

**vs other retrievers** (ContextBench, Python issues with annotated gold context — every row scored on the same set):

| retriever | file recall @10 | file recall @40 | line/span recall @10 |
|---|:--:|:--:|:--:|
| Aider repo-map (PageRank) | 0.11 | 0.27 | ~0.01 |
| BM25 (lexical) | 0.52 | 0.67 | ~0.01 |
| **VARD** | **0.60** | **0.79** | **0.61** |

~5× the Aider repo map at file level, and **50–70× better at the line-range level** — because VARD returns precise `file:start-end` spans, not whole files. (It is *not* trying to beat a full agentic search loop on file recall; those reach similar numbers but cost many LLM calls. VARD gets there in ~1s with no model.)

**Recovering data-coupled code** — the writer/reader on the *other side* of a cache key / DB row / queue that also breaks if you change one side. "Can each method find the coupled partner?" (top-10, on real apps):

| method | finds it | cross-module (the hard case) |
|---|:--:|:--:|
| grep / lexical | 52% | 26% |
| embeddings (SOTA) | 68% | 53% |
| call / import / inherit graph | 44% | 60% |
| **VARD resource graph** | **100%** | **100%** |

**12% of all couplings (20% cross-module) are found by VARD alone** — no text, embedding, or call-graph signal surfaces them. This is the part nothing else does.

**It generalizes.** Validated on Python (SWE-bench Verified) and Java / Spring Boot (two unseen apps). On a repo the models had **not** memorized: given only the issue text, a frontier model located the right file in **0 of 8** cases on its own; with VARD's retrieved context, **6 of 8** — and produced working fixes for several.

**Honest scope.** VARD is a localization/retrieval layer; the model still does the reasoning and the patch. Samples are small-to-medium (ContextBench n=30, SWE-bench Verified n=109); the resolution figures are directional, not a `%Resolved` leaderboard result (it isn't one, and isn't comparable to one).

## Install

```bash
git clone https://github.com/zero-iteration/vard && cd vard
pip install -e .                  # base: BM25 + symbol graph + data-coupling (no API key, fully local)
pip install -e ".[embeddings]"    # + local semantic embeddings (free, bge-small) — recommended
pip install -e ".[all]"           # + OpenAI option + MCP server + `vard learn`
```

Python 3.10+. The base install needs no API key and your code never leaves the machine. Without the `[embeddings]` extra VARD runs BM25 + graph + coupling and tells you it's doing so.

## Usage (CLI)

Run from inside the repo (path defaults to `.`):

```bash
vard init                                  # index it once (auto-refreshes on change; shows progress)
vard context "<bug or task>"               # relevant code + coupled partners, with reasons
vard couplings                             # list hidden writer⇄reader data couplings
vard impact OrderService.updateStatus      # blast radius before an edit
vard resource redis:status                 # who reads/writes a cache key / table / queue
vard learn                                 # optional: tune ranking weights from this repo's git history
```

## Use it with an agent (MCP)

VARD ships an MCP server so agents (Claude Code, Cursor, ...) can call it:

```bash
claude mcp add vard -- vard-mcp
```

Tools: `vard_context` (the main one), `vard_impact`, `vard_resource`, `vard_couplings`, `vard_index`, and agent-driven discovery (`vard_discovery_request` / `vard_set_ruleset`, so the agent itself supplies the resource ruleset — no key).

Optional pre-edit hook — warns the agent automatically when it edits code coupled through shared state:

```bash
vard install-hook            # current repo   (--global for all your repos)
```

## How it works

Two phases. Index time is heavy and runs once (incremental after); query time is ~1s.

```text
INDEX (once, cached in <repo>/.vard/):
  source → tree-sitter language providers → uniform symbols + call-sites
         → symbol graph (typed edges: contains, inherits)
         → DATA-RESOURCE layer:  writer ─writes→ cache:key ←reads─ reader   (implicit coupling made explicit)
         → commit-history mining  +  file-level import graph

QUERY (per task):
  score each symbol by its BEST-matching passage:  0.5·embedding + 0.5·BM25 over the whole method
    + commit-history (files similar past changes touched)
    + graph-PPR (files relevant code depends on, via the import graph)
    + optional HyDE (a hypothetical-code hint the agent can supply)
  → top-k precise spans · data-coupled partners · structurally-reachable candidates
```

Design notes worth knowing:

- **Passage-level units.** Each method is split into passages; a method scores by its single best-matching passage, so a few relevant lines inside a large method still surface instead of being averaged away.
- **Learned, weighted fusion.** Signal weights come from a learned reranker; a learned combination beats naive equal-weight fusion, so auxiliaries are weighted *below* the content backbone, never summed flat.
- **Language-agnostic core.** Everything above the tree-sitter providers is language-independent; adding a language is one config entry.
- **Stack-agnostic coupling.** VARD hardcodes no framework. It infers how *your* repo talks to caches/DBs/queues from its dependencies and call patterns (built-in heuristics by default; opt into an LLM with `VARD_DISCOVER=openai` or let the agent supply it).

### On the name — what "attention" means here

"Repository attention" is a metaphor for **selectively attending to the few relevant regions of a large repo** — it is *not* transformer attention (no query/key/value, no learned softmax over nodes). The component that genuinely plays that role is **relevance propagation over the import graph**: VARD seeds the files whose code matches the task, then lets that relevance *diffuse along dependency edges* via personalized PageRank, so a file is surfaced when relevant code depends on it even if its own text doesn't match. That is graph diffusion — the pre-neural cousin of attention (cf. [APPNP](https://arxiv.org/abs/1810.05997), "attention as personalized PageRank") — deterministic and ~instant.

We did build and test the learned, softmax-style alternatives (a trained logistic router, a task-conditioned GNN, a calibrated probability layer). None beat the simpler deterministic scoring + graph propagation, so VARD doesn't ship one. The real edge isn't a novel attention mechanism — it's the **data-coupling signal** (the table above) plus span-level retrieval.

## Languages

Python, Java (incl. Spring Boot), JavaScript / TypeScript (Node), Go.

## Configuration

| Env var | Meaning |
|---|---|
| `VARD_EMB_MODEL` | embedding backend. Default `BAAI/bge-small-en-v1.5` (local, free). `none` = BM25 only. `openai:text-embedding-3-large` = cloud. |
| `VARD_DISCOVER` | `openai` to opt into LLM-based resource discovery (default: free built-in heuristics, no API call). |
| `OPENAI_API_KEY` | only used if you opt into OpenAI embeddings/discovery (also read from `~/.config/vard/openai.key`). |
| `VARD_DEBUG` | `1` to print full tracebacks instead of one-line errors. |

## Security & privacy

- **No code execution.** VARD only parses your source statically; it never runs the code it analyzes.
- **Local and key-free by default.** Nothing leaves the machine unless you opt into OpenAI. When you do, only dependency manifests and call-pattern summaries are sent for discovery — never your full source.
- **The index is a local pickle** at `<repo>/.vard/index.pkl` (git-ignored by the provided `.gitignore`). As with any pickle, don't run `vard` against a `.vard/index.pkl` from an untrusted source — delete it and let VARD rebuild.

## Status

Early but working end-to-end: multi-language, self-maintaining incremental index, MCP server + pre-edit hook, key-free by default, graceful degradation (a bad file, missing dependency, or absent network never crashes it). Roadmap: cross-service coupling (the same idea across multiple repos / microservices).

## License

MIT © Shreyash Vardhan
