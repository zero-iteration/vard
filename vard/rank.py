"""Single source of truth for node ranking — used by both `context_text` (product) and the
benchmark, so they can never drift. Scores combine a content backbone (0.5·sem + 0.5·bm25) with
weighted auxiliaries: commit-history, graph-PPR (import propagation), and HyDE. Weights come from a
learned reranker (logistic, leave-one-instance-out CV on ContextBench); a learned ranker beat both
content-alone and naive equal-weight fusion, so auxiliaries are weighted *below* the content
backbone, not summed flat. `weights` is overridable (a per-repo self-labeling retrain can replace it)."""
import os
import re

# relative to the content composite (≙ 1.0)
# runtime weights: `runtime_edge` is the ADDITIVE rt_prox weight (relevance axis); `runtime` is λ, the
# MULTIPLICATIVE rt_conf strength (confidence axis) — a confirmed node is scaled by (1+λ). NOT learned from
# git (confirmation correlates with test coverage, not fix-likelihood); λ is calibrated from the eval.
DEFAULT_WEIGHTS = {"history": 0.6, "ppr": 0.45, "hyde_bm": 0.32, "hyde_sem": 0.10,
                   "runtime": 0.5, "runtime_edge": 0.6}
RUNTIME_MODES = ("off", "fused", "prior", "tag")
_IDENT = re.compile(r"[A-Za-z_]\w{2,}")
_BODY_CAP = 600                                         # body identifier tokens per node (whole method)

# Files rarely the FIX site — down-weighted (not excluded) so test/fixture/generated scaffolding
# doesn't crowd out the source where bugs actually live. A test can still surface if nothing else does.
_LOWREL_DIRS = ("/test/", "/tests/", "/__tests__/", "/testdata/", "/fixtures/", "/mocks/", "/mock/",
                "/examples/", "/example/", "/samples/", "/sample/", "/generated/", "/migrations/",
                "/migration/", "/node_modules/")
_LOWREL_SUFFIX = ("test.java", "tests.java", "it.java", "itcase.java", "_test.py", "_test.go",
                  ".test.js", ".test.ts", ".test.jsx", ".test.tsx", ".spec.js", ".spec.ts",
                  ".spec.jsx", ".spec.tsx", ".spec.java")
_LOWREL_PENALTY = 0.25


_EMB_WARNED = False


def _warn_no_embeddings(err):
    """Embeddings are half the ranking signal. If they fail we still return BM25-only results, but the
    user MUST know — silent degradation was the worst failure mode. Warn once per process."""
    global _EMB_WARNED
    if _EMB_WARNED:
        return
    _EMB_WARNED = True
    import sys
    msg = str(err)[:120]
    hint = ("install the embeddings extra:  pip install 'vard[embeddings]'"
            if "sentence_transformers" in msg or "No module" in msg
            else f"reason: {msg}")
    print(f"⚠ vard: embeddings unavailable — running in BM25-only mode (results will be weaker).\n"
          f"  {hint}\n  (or set VARD_EMB_MODEL=none to silence this and run lexical-only on purpose)",
          file=sys.stderr, flush=True)


def _relevance_prior(fpath):
    p = "/" + fpath.lower()
    if any(d in p for d in _LOWREL_DIRS):
        return _LOWREL_PENALTY
    base = p.rsplit("/", 1)[-1]
    if p.endswith(_LOWREL_SUFFIX) or base.startswith("test_") or base == "conftest.py":
        return _LOWREL_PENALTY
    return 1.0


def _mm(d):
    if not d:
        return {}
    v = list(d.values()); lo, hi = min(v), max(v)
    return {k: (x - lo) / (hi - lo) if hi > lo else 0.0 for k, x in d.items()}




def resolve_runtime_mode(idx, runtime_mode=None):
    """Pick the runtime ranking arm. Explicit mode wins; otherwise default to 'fused' when a trace
    exists for this repo, else 'off'. Modes: off / fused / prior / tag (see _apply_runtime)."""
    if runtime_mode in RUNTIME_MODES:
        return runtime_mode
    has_trace = bool(idx.get("rt_confirmed") or idx.get("rt_traced"))
    return "fused" if has_trace else "off"


def _runtime_proximity(content_node, rt_edges, top_seeds=15):
    """rt_prox — the RELEVANCE-axis runtime factor: content relevance propagated ONE hop along the
    ground-truth call graph, seeded sparsely by the strongest-content nodes (mirrors ppr's top_seeds to
    avoid long-tail diffusion). Higher precision than the file-import PPR — these are real, directed edges.
    Returns {node_id: [0,1]}, defined only on exercised neighbors of strong seeds."""
    if not rt_edges or not content_node:
        return {}
    seeds = dict(sorted(content_node.items(), key=lambda kv: kv[1], reverse=True)[:top_seeds])
    spread = {}
    for a, b, _n in rt_edges:
        if seeds.get(a, 0.0) > 0:
            spread[b] = spread.get(b, 0.0) + seeds[a]
        if seeds.get(b, 0.0) > 0:
            spread[a] = spread.get(a, 0.0) + seeds[b]
    hi = max(spread.values(), default=0.0)
    return {k: v / hi for k, v in spread.items()} if hi > 0 else {}


def _apply_runtime(score, content_node, idx, w, mode):
    """Fold the runtime overlay into `score` under `mode`, on TWO distinct axes (see rank research):

      rt_prox  (RELEVANCE axis)  — additive, weight w['runtime_edge']: real-call-graph proximity.
      rt_conf  (CONFIDENCE axis) — MULTIPLICATIVE, S·(1+λ·κ), λ=w['runtime']: a node we KNOW runs has its
                                   already-earned relevance amplified (it confirms, it doesn't discover).
                                   κ is binary today — under sampling, hit-counts ∝ on-CPU time, not call
                                   frequency, so we don't trust magnitudes until instrumentation (Tier 1b).

    Modes:  off   — no change (static baseline).
            fused — rt_prox additive + rt_conf multiplicative.
            prior — rt_conf multiplicative only.
            tag   — no scalar change (confidence surfaced by the renderer as a column, not in the score).

    INVARIANT (recall-safety): every term is non-negative-additive or a ≥1 multiplier, so for every node
    S_on(n) ≥ S_off(n). Runtime can only PROMOTE, never demote — it cannot hurt recall@k on untested gold.
    """
    if mode in ("off", "tag"):
        return score
    rt_conf = idx.get("rt_confirmed") or set()
    rt_edges = idx.get("rt_edges") or []
    if mode == "fused":                                  # relevance axis: real-call-graph proximity (additive)
        for nid, v in _runtime_proximity(content_node, rt_edges).items():
            if nid in score:
                score[nid] += w["runtime_edge"] * v
    if mode in ("fused", "prior"):                       # confidence axis: amplify confirmed-live code (×≥1)
        lam = max(0.0, w["runtime"])
        for nid in rt_conf:
            if nid in score:
                score[nid] *= (1.0 + lam)                # κ=1 (binary); g(hits) once instrumentation lands
    return score


def rank_nodes(idx, task, repo, nodes, hypothetical=None, weights=None, runtime_mode=None):
    """Return (score: {node_id: float}, hfiles: {file: history_score}). Pure scoring — no I/O
    beyond embeddings/PPR. `hfiles` is returned so callers can render the history section.
    `runtime_mode` selects the runtime arm (off/fused/prior/tag); None → auto (see resolve_runtime_mode)."""
    import re as _re
    from rank_bm25 import BM25Okapi
    from . import common as C, history as H
    if not nodes:                                    # empty / unsupported-language repo — BM25Okapi([]) would crash
        return {}, H.candidate_files(idx.get("history") or [], task)
    w = {**DEFAULT_WEIGHTS, **(weights or {})}
    idents, _ = C.extract_seeds(task)
    q = [t for s in idents for t in C.subtokens(s)] + C.subtokens(task.splitlines()[0] if task else "")
    import numpy as np
    from . import embed as E
    # PASSAGE-LEVEL units: split every method into pieces; the lexical (BM25) and semantic halves
    # index the SAME pieces, and a method is scored by its single BEST-matching piece. This lets a
    # few relevant lines inside a large multi-purpose method compete on their own, undiluted.
    chunks = E.node_chunk_texts(nodes, repo)
    keys, cdocs = [], []
    for n in nodes:
        qt = C.subtokens(n.qual)
        for t in chunks[n.id]:
            keys.append(n.id)
            cdocs.append(qt + [m.lower() for m in _IDENT.findall(t)][:_BODY_CAP])
    bm = BM25Okapi(cdocs)

    def _norm(a):
        a = np.asarray(a, float)
        lo, hi = a.min(), a.max()
        return (a - lo) / (hi - lo) if hi > lo else np.zeros_like(a)

    cbm = _norm(bm.get_scores(q or ["x"]))
    cvecs = None
    if os.environ.get("VARD_EMB_MODEL", "x") != "none":
        try:
            nv = E.embed_nodes(nodes, repo, "live")
            cvecs = np.vstack([nv[n.id][ci] for n in nodes for ci in range(len(chunks[n.id]))])
            csem = _norm(cvecs @ E.embed_task(task))
        except Exception as e:
            cvecs = None; csem = np.zeros(len(keys))
            _warn_no_embeddings(e)        # do NOT degrade silently — tell the user why results are weaker
    else:
        csem = np.zeros(len(keys))
    chunk_score = 0.5 * csem + 0.5 * cbm

    def _max_by_node(vals):                              # node value = its best piece
        out = {}
        for i, nid in enumerate(keys):
            if vals[i] > out.get(nid, -1.0):
                out[nid] = float(vals[i])
        return out

    content_node = _max_by_node(chunk_score)
    score = dict(content_node)
    # HyDE: same best-piece logic, scored against a hypothetical code snippet
    if hypothetical:
        hq = [t.lower() for t in _re.findall(r"[A-Za-z_]\w{2,}", hypothetical)]
        hbm = _norm(bm.get_scores(hq or ["x"]))
        if cvecs is not None:
            try:
                hsem = _norm(cvecs @ E.embed_texts([hypothetical])[0])
            except Exception:
                hsem = np.zeros(len(keys))
        else:
            hsem = np.zeros(len(keys))
        hyde_node = _max_by_node(w["hyde_bm"] * hbm + w["hyde_sem"] * hsem)
        for nid in score:
            score[nid] += hyde_node.get(nid, 0.0)
    # commit-history: lift nodes whose file similar past changes touched (additive recall)
    hfiles = H.candidate_files(idx.get("history") or [], task)
    if hfiles:
        for n in nodes:
            if n.file in hfiles:
                score[n.id] += w["history"] * hfiles[n.file]
    # graph-PPR: propagate content relevance over the import graph (file level)
    edges = idx.get("import_edges") or []
    if edges:
        from . import propagate as P
        file_cs = {}
        for n in nodes:
            file_cs[n.file] = max(file_cs.get(n.file, 0.0), content_node.get(n.id, 0.0))
        prn = P.ppr_scores(edges, list(file_cs.keys()), file_cs)
        for n in nodes:
            score[n.id] += w["ppr"] * prn.get(n.file, 0.0)
    # RUNTIME overlay — ground truth a static reader can't reconstruct (what actually ran + real call edges).
    # Folded on two axes via the selected arm; structurally monotonic (can only promote). See _apply_runtime.
    mode = resolve_runtime_mode(idx, runtime_mode)
    base = dict(score) if os.environ.get("VARD_VERIFY_INVARIANT") else None
    _apply_runtime(score, content_node, idx, w, mode)
    if base is not None:                                      # opt-in proof of the never-demote invariant
        bad = [nid for nid in base if score[nid] < base[nid] - 1e-9]
        assert not bad, f"runtime arm '{mode}' DEMOTED {len(bad)} nodes — invariant violated: {bad[:3]}"
    # down-weight files that are rarely the fix site (tests/fixtures/generated/migrations). Applied to BOTH
    # arms identically, so it preserves the on≥off inequality.
    for n in nodes:
        pr = _relevance_prior(n.file)
        if pr != 1.0:
            score[n.id] *= pr
    return score, hfiles
