# VARD research log — what we tried, what worked

VARD is a retrieval/context layer for coding agents. The hard cases are **coupling bugs**: code that
interacts through shared state (a cache key, a DB row, a serialized DTO, a message) with no direct call or
import between the two sides. Pure lexical/semantic search misses the far side because nothing textual
connects them.

We measure on **dark gold**: the parts of an accepted fix that are reachable by NONE of {lexical BM25,
semantic embeddings, 1-hop import from the query seeds}. Dark gold is the honest, held-out localization
target — it is exactly the code a normal retriever cannot find. Bugs are registered as `(repo, base SHA,
fix SHA, symptom-only issue text)`; gold is reconstructed from the diff and the repo is indexed at the
**parent** commit, so the fix is never in the index.

---

## Part 1 — construction attempts that did NOT move recall

We spent a long arc trying to change how the graph is *built* (nodes and edges), to widen recall without
collapsing precision. Each was implemented as a standalone module and measured against the same bugs. None
beat the existing retriever.

| Attempt | Idea | Result |
|---|---|---|
| **RWR (random-walk-with-restart / "gene" propagation)** | Replace/augment PPR with a restart-walk over the symbol graph to diffuse relevance from the query seeds | ≈ random. No lift over the current propagation. |
| **Bipartite / inverted-index (no explicit graph)** | Drop the graph data structure; model code↔resource as a bipartite inverted index and retrieve by shared resources | ≈ the current approach. Same recall, no structural advantage. |
| **Types-as-resources** | Treat every domain type as a "resource" with producers/consumers, not just cache/queue/db | Beats random only moderately, and only on one repo. Not a general win. |
| **Field-sensitive value-flow** (`T.f` write ↔ read) | Connect a field's write site to its read sites across files | High precision but far too sparse — fires on very few bugs. |
| **Intra-procedural def-use slicing** (ARISE-style) | Slice each method from the query-relevant line, forward+backward, return the slice instead of the whole method | NEGATIVE. Halves the span but loses ~45% of gold lines at every slice size. Our bugs live in small methods, so the large-method bottleneck slicing targets barely occurs, and many gold edits are not data-connected to the seed line. |
| **Multi-hop import (2/3-hop), call-graph 1-hop, inheritance expansion** | Widen the structural neighborhood around the seeds | Add NOTHING to the recall ceiling. The gap was never call/import distance. |
| **Co-change as a coupling signal** | Use git co-change history as a proxy for data coupling | Confounded. Most co-change is not data coupling; it inflates retro-runs on indexed history and is the wrong proxy to optimize against. |
| **Heuristic re-ranking of a wide pool** (merged-graph / ranked-union) | Build a wide candidate set, then re-rank it with signal-weighted boosts | ≤ the content baseline. Boosts promote noise; heuristic ranking of the pool fails. |

**Meta-conclusion.** VARD's coupling/state signal is **high-precision / low-recall**: when it fires it is
right (MRR 0.85–1.0 across every experiment), but it rarely fires, and no construction tweak moved that.
The reason is structural: the bugs are small and individually findable, co-change is a confounded proxy,
and lightweight static analysis loses more gold than it adds. Tuning *construction* against these proxies
was the wrong lever.

---

## Part 2 — what worked: a recall-complete, provenance-tagged pool

The shift that worked was a **product-shape** change, not a construction change:

> VARD does not try to *rank* the answer. It assembles a **recall-complete candidate pool** where every
> candidate is tagged with **why** it is there (content / resource-coupled / state-producer / field-flow /
> import-1hop / co-changed×N / config-anchor / package-sibling), and the agent selects from it.
> **Recall comes from the pool; precision comes from the agent.**

Heuristic ranking of the pool fails (Part 1), but the pool itself can be made to *contain* the dark gold,
and the provenance tags give the agent a reason to trust each candidate.

### Results — curated coupling bugs (n=11)

| Metric | Value |
|---|---|
| `codefirst@8` recall (the content ranker at top-8) | **0.18** |
| Pool ceiling (gold present in the pool) | **1.00** |
| Pool size (fraction of all symbols) | **~7%** avg |
| Hard floor (gold reachable by no signal) | **0%** |

Getting the ceiling from ~0.91 to 1.00 needed two signals we were missing, both about **co-location**, not
graph distance: a **configuration-anchor** signal (the cross-cutting wiring classes that no proximity signal
reaches) and **package-siblings of strong candidates** (content-dark gold is overwhelmingly co-located with
a strong candidate, inside a focused domain package). Multi-hop import / call / inheritance added nothing.

### Result — a new, previously-unseen repository (true dark-gold)

To check it generalizes, we registered a fresh bug on a repo outside the curation set: a multi-file
data-coupling fix where stale cached data breaks a downstream check. Symptom-only issue text, indexed at the
parent commit.

| Metric | Value |
|---|---|
| `codefirst@8` recall | **0.00** — pure retrieval finds *none* of the gold at top-8 |
| VARD pool ceiling | **0.89** — 8 of 9 gold recovered |

The single miss is a content-dark symbol whose only strong link is to another gold symbol that ranked just
below the seed cutoff. It is a known, principled limitation (the seed-count cap gates the expansion), not a
new failure mode — and we are deliberately not patching it with a one-off constant.

---

## Part 3 — robustness: no more silent drops

While hardening this we kept hitting the same class of bug: a hardcoded cap that **silently drops** a
candidate once a collection crosses a threshold. An audit found 20 such sites in the pool path, in three
recurring patterns:

- **A — truncating a recovery signal by the score it was built to bypass** (e.g. ranking the import or
  package-sibling expansion by content score, which re-drops the exact content-dark gold those signals
  exist to catch).
- **B — a hard cliff on a raw absolute count** (`≤40`, `≤80`, top-40), which is wrong for repos that vary
  in size by 100×, and is *why* the magic numbers kept needing re-tuning.
- **C — a weak-evidence check that drops an edge instead of weakening it.**

We fixed the root, not the numbers, with one invariant:

> **The pool layer tags and weights. It never silently drops.** Recovery signals are ranked by their own
> strength (never by content score), every cap is scale-relative, and truncation for precision happens at
> the agent/output layer, where it belongs.

Recall held at 1.00 on the curated set after the change (verified on a from-scratch rebuild, not a cached
index).

---

The durable finding: **on coupling bugs where lexical/semantic retrieval scores ~0, a recall-complete
provenance pool recovers ~90–100% of the hidden code** — and the value is the pool + its provenance, not any
single graph-construction trick.
