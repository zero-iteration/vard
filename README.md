# VARD

**A fast, local retrieval + memory layer for AI coding agents.** It indexes your whole project once,
then answers — in ~1s, with no LLM in the loop — *where the relevant code is*, *what data/state a task
touches*, *what's coupled through shared data* (caches, DBs, queues), and *why the code is the way it
is* (decisions/tickets/incidents from history). It finds context; the model still does the reasoning
and the edit. Local and key-optional by default.

## Contents

- [Why](#why)
- [Install](#install)
- [Quick start](#quick-start)  — index, then query
- [What VARD gives the agent](#what-vard-gives-the-agent)
- [Memory (code-anchored, self-invalidating)](#memory-code-anchored-self-invalidating)
- [Multi-module projects & dependencies](#multi-module-projects--dependencies)
- [How it works](#how-it-works)
- [Results](#results)
- [Configuration](#configuration)
- [Security & privacy](#security--privacy)
- [Status](#status)

## Why

When you ask an agent to change code, two things go wrong that a flat search (grep / embeddings) can't
fix:

1. **Invisible couplings.** A background job writes a value to a cache key; a handler reads it later.
   Change one and the other breaks — yet there's no call, no import, no shared text, no semantic
   similarity between them. Every text/embedding/call-graph retriever is blind to this.
2. **Missing the whole picture.** The agent reads the code but not the *state* it flows through, the
   readers it would break, or the decision/incident that shaped it — context that isn't reconstructable
   from the file in front of it.

VARD builds a queryable index that makes those explicit, so the agent starts from the right place
instead of rediscovering it (expensively) every time.

## Install

```bash
git clone https://github.com/zero-iteration/vard && cd vard
pip install -e .                  # base: BM25 + symbol graph + state + data-coupling (no key, fully local)
pip install -e ".[embeddings]"    # + local semantic embeddings (free, bge-small) — recommended
pip install -e ".[all]"           # + OpenAI option + MCP server + `vard learn`
```

Python 3.10+. The base install needs no API key and your code never leaves the machine.

## Quick start

**1 — Index** (run anywhere inside the project; it finds the project root and indexes every module):

```bash
vard init
```

`vard init` indexes the whole project, **writes a routing block to `CLAUDE.md`/`AGENTS.md`** so your
agent uses VARD automatically, and **registers the MCP server** (if Claude Code is present). It's
idempotent and re-indexes only changed files. (`--no-wire` to only index; `--fresh` to rebuild.)

**2 — Query.** Just describe a task to your agent and it calls VARD on its own. Or from the CLI:

```bash
vard context "<bug or task>"          # relevant code + data-coupled partners
vard whole-picture OrderService       # code + state + couplings + the why (decisions/tickets/incidents) + co-changes
vard impact OrderService.updateStatus # blast radius before an edit
vard resource redis:status            # who reads/writes a cache key / table / queue
```

## What VARD gives the agent

These are the MCP tools (and CLI equivalents) the agent calls. See [`AGENTS.md`](AGENTS.md) for the
agent-facing guide.

| tool | use it for |
|---|---|
| `vard_context(task)` | relevant code for a task, **including** the functions coupled through shared data. Call before grepping. |
| `vard_whole_picture(target)` | the full picture before editing: the code, the **state** it touches, the code **coupled** through shared data (what you'd break), the **decisions/tickets/incidents** behind it, and what **co-changes** with it. |
| `vard_state_candidates(task)` → `vard_state_lineage(types)` | state-first localization: when data is wrong/stale/incomplete and the code that sets it isn't obvious, see the program's state types, identify the wrong one(s), then get the code that defines and **produces/consumes** that state — including producers in other modules with no textual link to the symptom. |
| `vard_impact(target)` | readers/writers coupled through caches/DBs/queues that an edit would affect. |
| `vard_resource(name)` | who writes vs reads a given cache key / table / queue. |
| `vard_couplings()` | all implicit writer⇄reader data couplings in the repo. |
| `vard_remember(fact, citations)` | persist a durable fact that **isn't in the code** — a decision, constraint, gotcha, or correction the user stated ("this cache is the source of truth, not the DB"). Anchored to the cited code and auto-invalidated when that code changes. |
| `vard_recall(task)` | the remembered facts about code relevant to a task, each **freshness-checked** against the current code (✓ valid · ⚠ cited code changed, re-check). |

Optional hooks — install once, then VARD works without the agent having to ask:

```bash
vard install-hook            # current repo   (--global for all your repos)
```

- **pre-edit (impact):** warns when the agent edits code coupled through shared state.
- **on-prompt (memory):** deterministically recalls relevant remembered facts into context, and captures explicit user assertions — so memory doesn't depend on the agent choosing to call a tool.

### Memory (code-anchored, self-invalidating)

The durable knowledge an agent can't reconstruct from code — *why* it's this way, the decision, the gotcha — lives in `.vard/memory.json` as facts **anchored to a code symbol**. The rule is **anchor-or-drop**: a fact with no resolvable citation is refused, because an unanchorable claim can't be verified. Invalidation rides on the code itself — on recall, VARD re-checks the cited symbol:

- unchanged → **✓ active**
- changed → **⚠ stale** (surfaced for re-check, never asserted silently)
- deleted → **dropped**

That last part is the point: a memory that can't go stale without saying so, rather than a generator of confident, authoritative-sounding lies. Storage is plain JSON (human-editable); embeddings are only a fallback recall index.

## Multi-module projects & dependencies

`vard init` run anywhere in a Maven/Gradle project walks up to the **reactor / root project** and
indexes **all** modules — so cross-module couplings, state, and history live in one graph (bounded by
the git root; `VARD_NO_REACTOR=1` to index only the current dir).

It also indexes **source dependencies it can find** — co-located modules/repos whose `artifactId`
matches a declared dependency — so the agent isn't blind to a dependency module's code. Add roots
explicitly when they live elsewhere:

```bash
vard init --with ../shared-lib --with ../another-service
```

Only **source** is indexed (tree-sitter needs source); a binary-only jar with no local source isn't.

## How it works

Two phases. Indexing is heavy and runs once (incremental after); queries are ~1s with no model.

```text
INDEX (once, cached in <repo>/.vard/):
  source → tree-sitter providers → symbols + call-sites
         → SYMBOL graph        (typed edges: contains, inherits)
         → STATE graph         (types/fields as the data; producers/consumers via def-use)
         → DATA-RESOURCE layer (writer ─writes→ cache:key ←reads─ reader: implicit coupling made explicit)
         → commit-history mining + file-level import graph

QUERY (per task):
  rank symbols by best-matching passage (0.5·embedding + 0.5·BM25 over the method)
    + commit-history + import-graph propagation
  → precise file:line spans · data-coupled partners · (on request) state lineage + the why
```

Notes worth knowing:
- **Passage-level units** — a method scores by its single best-matching passage, so a few relevant
  lines in a large method still surface instead of being averaged away.
- **State is the spine for "wrong-data" bugs** — every type is state; code attaches as what produces/
  consumes it. The agent names the implicated state (reasoning), VARD traverses it (structure).
- **Stack-agnostic coupling** — no framework hardcoded; built-in heuristics by default, or opt into an
  LLM ruleset with `VARD_DISCOVER=openai`, or let the agent supply one.

## Results

VARD's core metric is **file localization**: given an issue, are the files that need changing in the
top-k? On **SWE-bench Verified** (109 real GitHub issues; gold = the files the accepted patch edits),
free local embeddings, ~1s/query, no LLM in the loop:

| top-k | found the right file (any@k) | recall@k |
|------:|:----------------------------:|:--------:|
|   5   | 0.67 | 0.64 |
|  10   | **0.75** | **0.74** |
|  20   | 0.84 | 0.83 |

**vs other retrievers** on **ContextBench** (public benchmark, human-annotated gold, same set):

| retriever | file recall @10 | line/span recall @10 |
|---|:--:|:--:|
| Aider repo-map | 0.11 | ~0.01 |
| BM25 (lexical) | 0.52 | ~0.01 |
| **VARD** | **0.60** | **0.61** |

~5× the Aider repo-map at file level and far higher at the line level, because VARD returns precise
`file:start-end` spans, not whole files.

**Recovering data-coupled code** — the writer/reader on the other side of a cache key / DB row / queue
(top-10, on real apps). This is the part nothing else does:

| method | finds the partner | cross-module (the hard case) |
|---|:--:|:--:|
| grep / lexical | 52% | 26% |
| embeddings | 68% | 53% |
| call / import graph | 44% | 60% |
| **VARD resource graph** | **100%** | **100%** |

**Token cost.** Localizing the relevant code costs VARD ~0 LLM tokens (it's local + deterministic),
versus an agent that searches and reads files to find the same code — tens of thousands of tokens per
task. VARD hands the model the relevant spans directly, so less of the context window is spent on
search and noise.

**Scope & honesty.** VARD finds context; the model reasons and patches. Localization (above) is the
trusted, held-out metric. The state-lineage and whole-picture layers are validated on real bugs but on
small samples — treat them as mechanism-proven, not leaderboard numbers. Coupling-class bugs are rare
in public history, so that evidence is necessarily small-n.

## Configuration

| Env var | Meaning |
|---|---|
| `VARD_EMB_MODEL` | embedding backend. Default `BAAI/bge-small-en-v1.5` (local, free). `none` = BM25 only. `openai:text-embedding-3-large` = cloud. |
| `VARD_DISCOVER` | `openai` to opt into LLM-based resource discovery (default: free built-in heuristics, no API call). |
| `VARD_NO_REACTOR` | `1` to index only the given directory instead of walking up to the project root. |
| `VARD_NO_DEPS` | `1` to skip auto-discovery of co-located source dependencies. |
| `OPENAI_API_KEY` | only used if you opt into OpenAI embeddings/discovery (also read from `~/.config/vard/openai.key`). |
| `VARD_DEBUG` | `1` to print full tracebacks instead of one-line errors. |

Languages: Python, Java (incl. Spring Boot), JavaScript / TypeScript (Node), Go.

## Security & privacy

- **No code execution.** VARD parses source statically; it never runs the code it analyzes.
- **Local and key-free by default.** Nothing leaves the machine unless you opt into OpenAI; even then,
  only dependency manifests and call-pattern summaries are sent for discovery — never your full source.
- **The index is a local pickle** at `<repo>/.vard/index.pkl` (git-ignored). As with any pickle, don't
  run `vard` against a `.vard/index.pkl` from an untrusted source — delete it and let VARD rebuild.

## Status

Early but working end-to-end: multi-language, multi-module, self-maintaining incremental index, MCP
server + pre-edit hook, key-free by default, graceful degradation (a bad file, missing dependency, or
absent network never crashes it). Roadmap: a write path for curated, non-reconstructable knowledge
(decisions, business rules, incidents) appended to the same graph; cross-service coupling.

## License

MIT © Shreyash Vardhan
