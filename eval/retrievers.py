"""Retrievers under test. Each returns a set of node ids (the top-k it would hand the agent).

Plug new retrievers here — the state-lineage graph will be one more function with the same
signature, scored by the same dark-gold metric so versions never drift.
"""
from vard import rank as RK, selflabel as SL
from . import channels as CH


def _top(score, k):
    return sorted(score, key=score.get, reverse=True)[:k]


def vard_codefirst(idx, task, repo, k=8):
    """The shipped fusion: content backbone + history + graph-PPR + (no HyDE here). The baseline
    we expect to recover ~0 dark gold, since it only re-weights the conventional channels."""
    rg = idx["rg"]
    nodes = CH.content_nodes(rg)
    score, _ = RK.rank_nodes(idx, task, repo, nodes, hypothetical=None, weights=SL.load_weights(repo))
    return set(_top(score, k))


def vard_with_coupling(idx, task, repo, k=8):
    """Code-first top-k PLUS the data-coupled partners of those hits (mirrors context_text).
    This is the only shipped signal that *can* reach dark gold — how much does it?"""
    rg = idx["rg"]
    nodes = CH.content_nodes(rg)
    score, _ = RK.rank_nodes(idx, task, repo, nodes, hypothetical=None, weights=SL.load_weights(repo))
    top = _top(score, k)
    out = set(top)
    res = idx.get("res") or {}
    writers, readers = res.get("writers", {}), res.get("readers", {})
    fn2res = {}
    for rid in res.get("nodes", []):
        for f in writers.get(rid, []) + readers.get(rid, []):
            fn2res.setdefault(f, set()).add(rid)
    for nid in top:
        for rid in fn2res.get(nid, ()):
            for p in set(writers.get(rid, [])) | set(readers.get(rid, [])):
                out.add(p)
    return out


def state_lineage(idx, task, repo, k=8):
    """The state-first retriever (eval/state_lineage.py): resource -> state type -> def + producers
    + consumers + interface/impl. The candidate to beat codefirst/coupling on dark gold."""
    from . import state_lineage as SLG
    return SLG.retrieve(idx, task, repo, k=k)


def merged_graph(idx, task, repo, k=8):
    """THE merge hypothesis: one graph = code (import/call) edges + state edges. We inject the state
    edges into VARD's propagation graph and run the SHIPPED ranker unchanged, so content relevance
    propagates through shared state just like it does through imports. Returns a tight top-k (same
    budget as codefirst) — the test is whether dark/coupling gold rises into it via propagation while
    logic-bug ranking is unharmed."""
    from . import state_lineage as SLG
    se = SLG.state_file_edges(idx, repo)
    idx2 = dict(idx)
    idx2["import_edges"] = list(idx.get("import_edges") or []) + se
    nodes = CH.content_nodes(idx["rg"])
    score, _ = RK.rank_nodes(idx2, task, repo, nodes, weights=SL.load_weights(repo))
    return set(_top(score, k))


def hybrid(idx, task, repo, k=8):
    """The unified retriever: ONE graph, TWO modes. Content/diffusion ranking is the spine (k tight
    hits, covers logic bugs); the typed state-traversal closure is unioned in ONLY when a state
    resource is implicated (covers coupling/dark gold). On a pure logic bug the state half returns {}
    so this collapses to codefirst — tight precision; on a coupling bug it adds the dark shapes."""
    from . import state_lineage as SLG
    rg = idx["rg"]
    nodes = CH.content_nodes(rg)
    score, _ = RK.rank_nodes(idx, task, repo, nodes, weights=SL.load_weights(repo))
    seeds = _top(score, k)
    return set(seeds) | SLG.gated_state_closure(idx, task, repo, seeds)


def general_hybrid(idx, task, repo, k=8):
    """Corrected unified retriever: content ranking (owns directly-named code + primitive/local state)
    UNION the GENERAL state closure (all types are state; coupling is just one shape). The state half
    is no longer gated on resource annotations."""
    from . import state_lineage as SLG
    rg = idx["rg"]
    nodes = CH.content_nodes(rg)
    score, _ = RK.rank_nodes(idx, task, repo, nodes, weights=SL.load_weights(repo))
    seeds = _top(score, k)
    return set(seeds) | SLG.general_state_closure(idx, task, repo, seeds)


def agent_state_hybrid(idx, task, repo, k=8):
    """The synthesis: content ranking spine UNION VARD-traversal of the state the AGENT identified
    from the symptom (pre-computed, read by identify.agent_state_closure). Tests whether LLM
    identification + VARD traversal recovers dark gold that heuristic implication could not."""
    from . import identify as ID
    rg = idx["rg"]
    nodes = CH.content_nodes(rg)
    score, _ = RK.rank_nodes(idx, task, repo, nodes, weights=SL.load_weights(repo))
    seeds = _top(score, k)
    return set(seeds) | ID.agent_state_closure(idx, task, repo)


REGISTRY = {
    "codefirst": vard_codefirst,
    "coupling": vard_with_coupling,
    "state_lineage": state_lineage,
    "merged_graph": merged_graph,
    "hybrid": hybrid,
    "general_hybrid": general_hybrid,
    "agent_state_hybrid": agent_state_hybrid,
}
