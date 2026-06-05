#!/usr/bin/env python3
"""Recall-complete, PROVENANCE-TAGGED candidate pool — the shipped form of eval/candidates.py.

VARD's validated result (eval FINDINGS Run 20-23): heuristic ranking of a wide set fails, so instead VARD
assembles a recall-complete pool where every candidate is tagged with WHY it's there (content / resource-
coupled / state-producer / import-1hop / co-changed×N / config-anchor / package-sibling) and the calling
AGENT selects. Recall from the pool, precision from the agent. INVARIANT: the pool TAGS and WEIGHTS, it
never silently DROPS — recovery signals are ranked by their OWN strength (never by content score, which
would re-drop the content-dark gold they exist to catch); every cap is scale-relative.

This is a high-recall CONTEXT provider for the host agent, not a localizer that out-reasons it.
"""
import collections
from . import state as ST, rank as RK, selflabel as SL, propagate as P, memory as MEM
from .languages import profiles


def _content(rg):
    return ST._content_nodes(rg)


def _resource_partners(idx, seeds):
    rg = idx["rg"]; res = idx.get("res") or {}
    sfiles = {rg.nodes[i].file for i in seeds if i in rg.nodes}
    w, r = res.get("writers", {}), res.get("readers", {})
    out = set()
    for rid in res.get("nodes", []):
        owners = set(w.get(rid, [])) | set(r.get(rid, []))
        if any(o in seeds or (o in rg.nodes and rg.nodes[o].file in sfiles) for o in owners):
            out |= {o for o in owners if o in rg.nodes}
    return out


def _import_1hop(idx, seeds, cap):
    """1-hop import neighbours, bounded by ranking neighbour files on import-centrality (#seeds connected) —
    NOT by content score (that re-drops content-dark gold)."""
    rg = idx["rg"]; adj = P.undirected_adj(idx.get("import_edges") or [])
    sfiles = {rg.nodes[i].file for i in seeds if i in rg.nodes}
    deg = collections.Counter()
    for f in sfiles:
        for nb in adj.get(f, set()):
            deg[nb] += 1
    keep = {f for f, _ in deg.most_common(cap)}
    return {n.id for n in _content(rg) if n.file in keep}


def _cochange(idx, repo, seeds, cap):
    rg = idx["rg"]; mem = MEM.mine_changes(repo)
    sfiles = {rg.nodes[i].file for i in seeds if i in rg.nodes}
    cnt = collections.Counter()
    for f in sfiles:
        for x, c in mem["cochange"].get(f, {}).items():
            cnt[x] += c                              # floor>=1: the count carries the confidence
    co = {n.id: cnt[n.file] for n in _content(rg) if n.file in cnt}
    return dict(sorted(co.items(), key=lambda kv: -kv[1])[:cap])


def candidate_pool(idx, task, repo, content_score, seeds, content_n=30):
    """{node_id: {qual,file,lines,tags}} — recall-complete, every entry tagged with why."""
    rg = idx["rg"]
    nodes_all = _content(rg)
    total = max(1, len(nodes_all))
    sg = idx.get("state") or ST.build_state_graph(rg, repo)
    res = _resource_partners(idx, seeds)
    st = ST.lineage(sg, rg, ST.auto_implicated(sg, rg, task, seeds))
    sig_cap = max(60, total // 150)                  # scale-relative, not a bare constant
    imp = _import_1hop(idx, seeds, sig_cap)
    co = _cochange(idx, repo, seeds, sig_cap)
    pool = {}

    def add(cid, tag):
        if cid not in rg.nodes:
            return
        n = rg.nodes[cid]
        pool.setdefault(cid, {"qual": n.qual.split("::")[-1], "file": n.file,
                              "lines": f"{n.start}-{n.end}", "tags": []})
        pool[cid]["tags"].append(tag)

    for i, cid in enumerate(sorted(content_score, key=content_score.get, reverse=True)[:content_n]):
        add(cid, f"content#{i+1}")
    for cid in res:
        add(cid, "resource-coupled")
    for cid in st:
        add(cid, "state-producer")
    for cid in imp:
        add(cid, "import-1hop")
    for cid, c in co.items():
        add(cid, f"co-changed x{c}")
    # config-anchor: cross-cutting wiring classes (@Configuration/@Enable*/@Module) — fix sites no
    # proximity signal reaches.
    prof = profiles.dominant_profile(rg)
    deco = getattr(rg, "node_decorators", {})
    anchor_ids = {nid for nid, decs in deco.items() if prof.is_config_anchor(decs)}
    for nid in anchor_ids:
        add(nid, "config-anchor")
    # package-siblings of strong candidates in FOCUSED packages (content-dark gold is co-located); no
    # content cap (capping re-drops the dark gold this exists to catch). focus_bound scales with repo size.
    strong = set(seeds) | res | st | anchor_ids
    dir_counts = collections.Counter(__import__("os").path.dirname(n.file) for n in nodes_all)
    sib_dirs = {__import__("os").path.dirname(rg.nodes[s].file) for s in strong if s in rg.nodes}
    focus_bound = max(80, total // 150)
    import os
    for n in nodes_all:
        d = os.path.dirname(n.file)
        if d in sib_dirs and dir_counts[d] <= focus_bound:
            add(n.id, "package-sibling")
    return pool


def pool_text(idx, task, repo, budget=120):
    """Render the pool for the agent: provenance-tagged, richest-first, capped to a context budget."""
    rg = idx["rg"]
    nodes = _content(rg)
    cs, _ = RK.rank_nodes(idx, task, repo, nodes, weights=SL.load_weights(repo))
    seeds = set(sorted(cs, key=cs.get, reverse=True)[:8])
    pool = candidate_pool(idx, task, repo, cs, seeds)
    if not pool:
        return f"# No candidates for: {task}"
    # order: most signals first (multi-signal = higher confidence), then content score
    def keyf(item):
        cid, v = item
        return (-len(v["tags"]), -cs.get(cid, 0.0))
    items = sorted(pool.items(), key=keyf)
    out = [f"# Recall-complete candidates for: {task}",
           f"# {len(pool)} candidates, each tagged with WHY. This is a HIGH-RECALL set — pick the relevant "
           f"ones; don't assume all apply. Showing top {min(budget, len(pool))} by signal strength.\n"]
    for cid, v in items[:budget]:
        out.append(f"- {v['file']}:{v['lines']}  {v['qual']}  [{', '.join(v['tags'])}]")
    if len(pool) > budget:
        out.append(f"\n# … {len(pool) - budget} more lower-signal candidates (raise the budget to see them).")
    return "\n".join(out)
