# VARD

**A fast, local context + memory layer for AI coding agents, with an optional ground-truth runtime layer.**
It indexes your whole project once, then answers — in ~1s, with no LLM in the loop — *where the relevant
code is*, *what data/state a task touches*, *what's coupled through shared data* (caches, DBs, queues), and
*why the code is the way it is* (decisions/tickets/incidents from history). Then, optionally, it observes
your app **actually running** — under your tests or attached to a live process — to ground all of that in
*what really executed, with what values, and what state it changed*. It finds and grounds context; the
model still does the reasoning and the edit. Local and key-optional by default.

## Contents

- [Why](#why)
- [Two layers](#two-layers)
- [Install](#install)
- [Quick start](#quick-start)
- [What VARD gives the agent](#what-vard-gives-the-agent)
- [Memory (code-anchored, self-invalidating)](#memory-code-anchored-self-invalidating)
- [Runtime layer (ground truth from observed execution)](#runtime-layer-ground-truth-from-observed-execution)
- [`explain` — actual vs expected](#explain--actual-vs-expected)
- [Multi-module projects & dependencies](#multi-module-projects--dependencies)
- [How it works](#how-it-works)
- [Results](#results)
- [Configuration](#configuration)
- [Security & privacy](#security--privacy)
- [Status](#status)

## Why

When you ask an agent to change code, several things go wrong that a flat search (grep / embeddings) can't
fix:

1. **Invisible couplings.** A background job writes a value to a cache key; a handler reads it later.
   Change one and the other breaks — yet there's no call, no import, no shared text, no semantic
   similarity between them. Every text/embedding/call-graph retriever is blind to this.
2. **Missing the whole picture.** The agent reads the code but not the *state* it flows through, the
   readers it would break, or the decision/incident that shaped it — context that isn't reconstructable
   from the file in front of it.
3. **Confident-wrong about runtime.** What the code *says* and what it *does* diverge: a config file
   defaults one way but the live value is overridden; the path the agent reasons about isn't the one that
   actually runs. Reading the source can't tell you which is true at runtime.

VARD makes (1) and (2) explicit from a static index, and grounds (3) by observing real execution — so the
agent starts from the right place, and from facts instead of guesses.

## Two layers

- **Static layer** (always on, multi-language, no execution): a queryable index of code, state, implicit
  data couplings, config, commit history, and code-anchored memory. Answers *where / what / why* in ~1s.
- **Runtime layer** (optional, opt-in, JVM today): a low-overhead agent observes the app while **your own
  tests run** or while it's **attached to a running process** — recording what actually executed, the real
  call graph, observed argument/return **values**, **state changes** (before→after), and **live config
  values**. It never runs anything itself; it only listens to execution you already drive.

The runtime layer *grounds* the static layer (confirms or corrects its inferences) and powers `explain` —
a single answer that contrasts how the code **actually runs** with what you **expected**, every line tagged
with how it's known.

## Install

```bash
git clone https://github.com/zero-iteration/vard && cd vard
pip install -e .                  # base: BM25 + symbol graph + state + data-coupling (no key, fully local)
pip install -e ".[embeddings]"    # + local semantic embeddings (free, bge-small) — recommended
pip install -e ".[all]"           # + OpenAI option + MCP server + self-tuning weights
```

Python 3.10+. The base install needs no API key and your code never leaves the machine.

**Optional — runtime layer (JVM apps).** Build the observation agent once (needs a JDK + Maven):

```bash
bash vard-agent/build.sh          # produces the agent jar (built for JDK 11 bytecode; loads on 11+)
```

You only need this if you want the runtime/`explain` features on a JVM project. Everything else works
without it.

## Quick start

**1 — Index** (run anywhere inside the project; it finds the project root and indexes every module):

```bash
vard init
```

`vard init` indexes the whole project, **writes a routing block to `CLAUDE.md`/`AGENTS.md`** so your agent
uses VARD automatically, and **registers the MCP server** (if Claude Code is present). It's idempotent and
re-indexes only changed files. (`--no-wire` to only index; `--fresh` to rebuild.)

**2 — Query (static).** Just describe a task to your agent and it calls VARD on its own. Or from the CLI:

```bash
vard context "<bug or task>"            # relevant code + data-coupled partners
vard whole-picture OrderService         # code + state + couplings + the why (history) + co-changes
vard impact OrderService.updateStatus   # blast radius before an edit
vard resource cache:order               # who reads/writes a cache key / table / queue
vard config feature.flags.checkout      # where a config key is defined (per profile) + the code that reads it
```

**3 — Ground it at runtime (optional, JVM).** Run your existing tests, or attach to a running process,
under the observation agent — then ask how it actually ran:

```bash
vard test --env test -- mvn test        # run YOUR tests under the agent; merge what actually executed
vard attach <pid> --env staging --for 60  # attach to a running process for 60s, observe, merge
vard explain OrderService               # actual (observed) vs expected, with the divergence made explicit
vard coverage OrderService.updateStatus # did it run? executed / instrumented-but-never-ran / not-instrumented
```

## What VARD gives the agent

These are the MCP tools (and CLI equivalents) the agent calls. See [`AGENTS.md`](AGENTS.md) for the
agent-facing guide.

**Find & understand (static, no execution):**

| tool | use it for |
|---|---|
| `vard_context(task)` | relevant code for a task, **including** the functions coupled through shared data. Call before grepping. |
| `vard_candidates(task)` | the **recall-complete** candidate pool — every candidate tagged with WHY (content / resource-coupled / state-producer / import-1hop / co-changed×N / config-anchor / package-sibling). A tagged superset for hard "what else touches this?" cases: recall from the pool, precision from you. |
| `vard_whole_picture(target)` | the full picture before editing: the code, the **state** it touches, the code **coupled** through shared data (what you'd break), the **decisions/tickets/incidents** behind it, and what **co-changes** with it. |
| `vard_state_candidates(task)` → `vard_state_lineage(types)` | state-first localization for wrong/stale/incomplete-data bugs: see the program's state types, identify the wrong one(s), then get the code that defines and **produces/consumes** that state — including producers in other modules with no textual link to the symptom. |
| `vard_impact(target)` | readers/writers coupled through caches/DBs/queues that an edit would affect. |
| `vard_resource(name)` | who writes vs reads a given cache key / table / queue. |
| `vard_couplings()` | all implicit writer⇄reader data couplings in the repo. |
| `vard_config(query)` | the config/properties that change behaviour at **runtime** but aren't in the code — where a key is defined (across profiles, with values) and the code that reads it. A value→code coupling with no call/import link. |

**Remember & expect (code-anchored memory):**

| tool | use it for |
|---|---|
| `vard_remember(fact, citations)` | persist a durable fact that **isn't in the code** — a decision, constraint, gotcha, or correction the user stated. Anchored to the cited code and auto-invalidated when that code changes. |
| `vard_expect(expectation, citations)` | record what the user **expected** the code to do (the oracle side). `explain` contrasts it against what actually ran. |
| `vard_recall(task)` | the remembered facts about code relevant to a task, each **freshness-checked** against the current code (✓ valid · ⚠ cited code changed, re-check). |

**Ground & explain (runtime, JVM, opt-in):**

| tool | use it for |
|---|---|
| `vard_explain(target)` | one joined answer for a symbol/file/ticket: **ACTUAL** (what was observed running, with real values & state changes), **MECHANISM** (the code + the commit/ticket behind it), **EXPECTED** (what you recorded), **CONFIG** (file value vs the value observed live), **DIVERGENCE** (explicit conflicts), **UNCERTAINTY** (what couldn't be confirmed) — every line provenance-tagged. |
| `vard_coverage(target)` | did a method actually run? **executed** / **instrumented-but-never-ran** (a path no test/request reached — drive it) / **not-instrumented** (a real gap). Removes the "did it miss X, or just not get exercised?" ambiguity. |

Optional hooks — install once, then VARD works without the agent having to ask:

```bash
vard install-hook            # current repo   (--global for all your repos)
```

- **pre-edit (impact):** warns when the agent edits code coupled through shared state.
- **on-prompt (memory):** deterministically recalls relevant remembered facts into context, and captures
  explicit user assertions — so memory doesn't depend on the agent choosing to call a tool.

### Memory (code-anchored, self-invalidating)

The durable knowledge an agent can't reconstruct from code — *why* it's this way, the decision, the gotcha,
or *what you expected* — lives in `.vard/memory.json` as facts **anchored to a code symbol**. The rule is
**anchor-or-drop**: a fact with no resolvable citation is refused, because an unanchorable claim can't be
verified. Each fact carries a **kind** — `mechanism` (why it's coded this way), `expectation` (what you
expected / a correction), or `observation` — so the right facts feed the right side of `explain`.

Invalidation rides on the code itself — on recall, VARD re-checks the cited symbol:

- unchanged → **✓ active**
- changed → **⚠ stale** (surfaced for re-check, never asserted silently)
- deleted → **dropped**

That last part is the point: a memory that can't go stale without saying so, rather than a generator of
confident, authoritative-sounding lies. Storage is plain JSON (human-editable); embeddings are only a
fallback recall index.

## Runtime layer (ground truth from observed execution)

The static index infers; the runtime layer **observes**. You drive execution the way you already do —
VARD only listens — and it merges what it saw into the index as a high-confidence overlay.

```bash
vard test --env <label> -- <your test or run command>   # e.g. -- mvn test
vard attach <pid> --env <label> --for <seconds>          # attach to a running process, no restart
```

What it captures, all from real execution:

- **What actually ran** — the methods that executed, deterministically (including small/fast methods that
  sampling-based profilers miss), with true call counts and the **real call graph** (resolving dynamic
  dispatch / interface→impl that a static import graph can't).
- **Observed values** — bounded, type-safe argument/return values, with objects unfolded into their scalar
  fields, so the actual numbers behind a decision are visible (not just types).
- **State changes (mutations)** — what changed, key-by-key: domain field updates as `before → after`, and
  cache/DB/queue writes/reads keyed by the **real runtime key** — turning the static "writer ⇄ reader"
  guess into an observed *writer → key → reader* edge.
- **Live config values** — the value a config key actually resolved to at runtime, so a runtime override
  (env / config service / launch args) that differs from the committed files becomes visible.
- **Per-run provenance** — every observation is tagged with the run/profile label you pass (`--env`), so
  merging a test run and a staging run never conflates a test path with a production one. The overlay
  **accretes** across runs and is **freshness-anchored** to the code (an observation goes stale when the
  cited code changes, exactly like memory).

The overlay also strengthens ranking: code that's confirmed to actually run is promoted, and relevance
propagates along the real call graph — but only ever upward (a never-demote invariant), so turning the
runtime layer on can't hurt results on code your tests don't cover.

Safety envelope (see also [Security & privacy](#security--privacy)): it **observes only writes the app
already performs** — it never issues new writes, and never calls getters/arbitrary code to read state
(fields are read directly). Captured values are bounded (depth, size, element counts) and **secret-named
methods/fields are redacted**. Traces are local. Today the runtime layer targets the **JVM**; the static
and memory layers are language-agnostic.

### `explain` — actual vs expected

`vard explain <symbol | file | ticket>` is the join: it never claims to find the bug — it makes the
**divergence** between how the code actually runs and what you expected undeniable, with each line tagged by
how it's known:

```text
ACTUAL       what was OBSERVED running — methods, real call edges, observed values     [confirmed-runtime] / [observed-value]
STATE        what CHANGED — field before→after, and writer → key → reader on real keys  [mutation]
MECHANISM    the code, and the commit / ticket that introduced it                       [code] / [commit]
EXPECTED     what you recorded you expected                                             [your expectation]
CONFIG       the file value vs the value observed live                                  [config] / [observed-live]
DIVERGENCE   explicit, groundable conflicts (e.g. file says A but runtime ran B)        [divergence]
UNCERTAINTY  what could NOT be confirmed — stated, never guessed                        [unverified]
```

When the runtime overlay isn't present, `explain` degrades honestly: it shows the static legs and clearly
marks the runtime leg as unconfirmed.

## Multi-module projects & dependencies

`vard init` run anywhere in a Maven/Gradle project walks up to the **reactor / root project** and indexes
**all** modules — so cross-module couplings, state, and history live in one graph (bounded by the git root;
`VARD_NO_REACTOR=1` to index only the current dir).

It also indexes **source dependencies it can find** — co-located modules/repos whose `artifactId` matches a
declared dependency — so the agent isn't blind to a dependency module's code. Add roots explicitly when they
live elsewhere:

```bash
vard init --with ../shared-lib --with ../another-service
```

Only **source** is indexed (tree-sitter needs source); a binary-only jar with no local source isn't.

## How it works

Indexing is heavy and runs once (incremental after); queries are ~1s with no model. The runtime overlay is
optional and merges in when you run tests / attach.

```text
INDEX (once, cached in <repo>/.vard/):
  source → tree-sitter providers → symbols + call-sites
         → SYMBOL graph        (typed edges: contains, inherits)
         → STATE graph         (types/fields as the data; producers/consumers via def-use)
         → DATA-RESOURCE layer (writer ─writes→ cache:key ←reads─ reader: implicit coupling made explicit)
         → CONFIG layer        (keys read in code ⇄ where they're defined, across profiles)
         → commit-history mining + file-level import graph

QUERY (per task):
  rank symbols by best-matching passage (0.5·embedding + 0.5·BM25 over the method)
    + commit-history + import-graph propagation + (if present) runtime confirmation
  → precise file:line spans · data-coupled partners · (on request) state lineage + the why

RUNTIME OVERLAY (optional, opt-in, JVM):
  your tests / a live process → observation agent → executed methods · real call edges · observed values
         · state mutations (before→after) · live config values   (per-run, freshness-anchored)
  → grounds the static index and powers `explain` (actual vs expected) and `coverage`
```

Notes worth knowing:
- **Passage-level units** — a method scores by its single best-matching passage, so a few relevant lines in
  a large method still surface instead of being averaged away.
- **State is the spine for "wrong-data" bugs** — every type is state; code attaches as what produces/
  consumes it. The agent names the implicated state (reasoning), VARD traverses it (structure).
- **Stack-agnostic coupling** — no framework hardcoded; built-in heuristics by default, or opt into an LLM
  ruleset with `VARD_DISCOVER=openai`, or let the agent supply one.
- **Observed beats inferred** — where the runtime overlay exists, it confirms or corrects the static guess
  (a confirmed coupling, a real call edge, the actual config value), and never silently overrides what it
  hasn't seen.

## Results

VARD's core, held-out metric is **file localization**: given an issue, are the files that need changing in
the top-k? On **SWE-bench Verified** (109 real GitHub issues; gold = the files the accepted patch edits),
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

**Scope & honesty.** VARD finds and grounds context; the model reasons and patches. Localization (above) is
the trusted, held-out metric. The state-lineage, whole-picture, memory, and **runtime/`explain`** layers are
validated by mechanism on real projects but are not leaderboard-benchmarked — treat them as mechanism-proven,
not as numbers. The runtime layer's coverage is exactly what your tests/usage exercise; the static layer is
the recall floor underneath it.

## Configuration

| Env var | Meaning |
|---|---|
| `VARD_EMB_MODEL` | embedding backend. Default `BAAI/bge-small-en-v1.5` (local, free). `none` = BM25 only. `openai:text-embedding-3-large` = cloud. |
| `VARD_DISCOVER` | `openai` to opt into LLM-based resource discovery (default: free built-in heuristics, no API call). |
| `VARD_NO_REACTOR` | `1` to index only the given directory instead of walking up to the project root. |
| `VARD_NO_DEPS` | `1` to skip auto-discovery of co-located source dependencies. |
| `OPENAI_API_KEY` | only used if you opt into OpenAI embeddings/discovery (also read from `~/.config/vard/openai.key`). |
| `VARD_DEBUG` | `1` to print full tracebacks instead of one-line errors. |

Runtime-layer flags live on the commands: `vard test`/`vard attach` take `--env <label>` (run provenance),
`--debug` (log what the agent instrumented + surface any class it couldn't), and `--for`/`--flush` (attach
window + snapshot interval).

Languages: static + memory layers — Python, Java (incl. Spring Boot), JavaScript / TypeScript (Node), Go.
Runtime layer — JVM (Java/Kotlin and other JVM languages).

## Security & privacy

- **Static analysis never executes your code.** The static, state, coupling, config, and memory layers parse
  source only.
- **The runtime layer is observe-only.** It attaches to execution **you** drive (your tests, or a process
  you point it at) and records only writes the app already performs — it never issues new writes and never
  invokes getters/arbitrary code to read state (fields are read directly). Captured values are bounded
  (depth, size, element count) and **secret-named methods/fields are redacted**; traces are written locally.
  Attaching to a running process is local and same-user. Skip the runtime layer entirely and the rest is
  unaffected.
- **Local and key-free by default.** Nothing leaves the machine unless you opt into OpenAI; even then, only
  dependency manifests and call-pattern summaries are sent for discovery — never your full source.
- **The index is a local pickle** at `<repo>/.vard/index.pkl` (git-ignored). As with any pickle, don't run
  `vard` against a `.vard/index.pkl` from an untrusted source — delete it and let VARD rebuild.

## Status

Working end-to-end: multi-language static index (symbol + state + data-coupling + config + history), code-
anchored self-invalidating memory with typed facts, an optional JVM runtime layer (observed methods, call
graph, values, state mutations, live config — per-run and freshness-anchored), the `explain` actual-vs-
expected join, and a `coverage` diagnostic. Self-maintaining incremental index, MCP server + pre-edit/
on-prompt hooks, key-free by default, graceful degradation (a bad file, missing dependency, absent network,
or absent runtime overlay never crashes it). A regression test suite covers the core invariants. Roadmap:
broaden the runtime layer beyond the JVM; richer value/state divergence; cross-service coupling.

## License

MIT © Shreyash Vardhan
