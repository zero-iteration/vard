#!/usr/bin/env python3
"""Attack LOW RECALL by UNIONING precise signals (lever 1), measured on the curated bugs.

The session proved a wider net via dense/multi-hop edges collapses precision. So instead union several
NARROW-but-precise signals we built and only tested in isolation, and see (a) how much recall each adds
over content alone, (b) the precision (output-size) cost, (c) which signal recovers the most missed gold.

Signals (each precise):
  content    : codefirst top-k (BM25+sem+history+import-PPR)
  +resource  : data-coupling partners (cache/queue/table writer<->reader) of the content seeds
  +state     : producer/consumer lineage of the implicated state (gated state closure)
  +field     : field-sensitive value-flow partners (T.f write<->read)
  +import    : 1-hop import neighbors of the content seeds
  +cochange  : files historically changed with the content seeds (history)
  UNION      : all of the above

  python -m eval.recall_union
"""
import sys, os, glob, collections
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vard import cli, rank as RK, selflabel as SL, propagate as P, state as ST, memory as MEM
from eval import dataset as D, metric as M, channels as CH, valueflow as VF


def _content_seeds(idx, task, repo, k=8):
    nodes = CH.content_nodes(idx["rg"])
    score, _ = RK.rank_nodes(idx, task, repo, nodes, weights=SL.load_weights(repo))
    return set(sorted(score, key=score.get, reverse=True)[:k])


def _resource_partners(idx, seeds):
    rg = idx["rg"]; res = idx.get("res") or {}
    sfiles = {rg.nodes[i].file for i in seeds}
    w, r = res.get("writers", {}), res.get("readers", {})
    out = set()
    for rid in res.get("nodes", []):
        owners = set(w.get(rid, [])) | set(r.get(rid, []))
        if any(o in seeds or (o in rg.nodes and rg.nodes[o].file in sfiles) for o in owners):
            out |= {o for o in owners if o in rg.nodes}
    return out


def _import_1hop(idx, seeds):
    rg = idx["rg"]; adj = P.undirected_adj(idx.get("import_edges") or [])
    sfiles = {rg.nodes[i].file for i in seeds}
    reach = set()
    for f in sfiles:
        reach |= adj.get(f, set())
    return {n.id for n in CH.content_nodes(rg) if n.file in reach}


def _import_1hop_ranked(idx, seeds, cap):
    """1-hop import neighbors, but bounded for monorepos by ranking neighbor FILES on import centrality
    (how many distinct seed files they connect to) — NOT by content score. Keeps the top-`cap` files and
    returns all their content nodes. A content-dark neighbor that several seeds import still ranks high."""
    rg = idx["rg"]; adj = P.undirected_adj(idx.get("import_edges") or [])
    sfiles = {rg.nodes[i].file for i in seeds}
    deg = collections.Counter()
    for f in sfiles:
        for nb in adj.get(f, set()):
            deg[nb] += 1
    keep = {f for f, _ in deg.most_common(cap)}
    return {n.id for n in CH.content_nodes(rg) if n.file in keep}


def _cochange(idx, repo, seeds):
    rg = idx["rg"]; mem = MEM.mine_changes(repo)
    sfiles = {rg.nodes[i].file for i in seeds}
    cofiles = set()
    for f in sfiles:
        cofiles |= {x for x, c in mem["cochange"].get(f, {}).items() if c >= 2}
    return {n.id for n in CH.content_nodes(rg) if n.file in cofiles}


def _field_partners(idx, repo, seeds):
    rg = idx["rg"]
    R = VF.field_resource_index(idx, repo)
    sfiles = {rg.nodes[i].file for i in seeds}
    partfiles = set()
    for rid, fm in R.items():
        if any(fl in sfiles for fl in fm):
            partfiles |= set(fm)
    return {n.id for n in CH.content_nodes(rg) if n.file in partfiles}


def _cochange_counts(idx, repo, seeds):
    """Co-change counts of seed files. Floor is >=1 (a single co-change is real evidence for a
    rarely-touched file — was >=2, which silently dropped it); the count is returned so callers rank by it
    and a scale-relative cap bounds the pool, rather than a hard count cliff deciding membership."""
    rg = idx["rg"]; mem = MEM.mine_changes(repo)
    sfiles = {rg.nodes[i].file for i in seeds}
    cnt = collections.Counter()
    for f in sfiles:
        for x, c in mem["cochange"].get(f, {}).items():
            if c >= 1:
                cnt[x] += c
    return {n.id: cnt[n.file] for n in CH.content_nodes(rg) if n.file in cnt}


def ranked_union(idx, task, repo, content_score, seeds, precise_only=False):
    """Rank the recall-complete pool. FIXED principled weights (not tuned to the test): precise signals
    (resource/state/field) > broad (import/cochange); a CONSENSUS bonus for candidates flagged by several
    independent signals. precise_only: drop the noisy broad signals from BOTH pool and scoring."""
    rg = idx["rg"]
    sg = idx.get("state") or ST.build_state_graph(rg, repo)
    res = _resource_partners(idx, seeds)
    st = ST.lineage(sg, rg, ST.auto_implicated(sg, rg, task, seeds))
    fld = _field_partners(idx, repo, seeds)
    imp = set() if precise_only else _import_1hop(idx, seeds)
    co = {} if precise_only else _cochange_counts(idx, repo, seeds)
    maxco = max(co.values()) if co else 1
    pool = set(seeds) | res | st | fld | imp | set(co)
    # rerank the FULL content ranking with signal boosts (so it can only add, never lose content gold)
    allids = [n.id for n in CH.content_nodes(rg)]
    vals = [content_score.get(c, 0.0) for c in allids]
    lo, hi = (min(vals), max(vals)) if vals else (0.0, 1.0)
    cn = lambda c: (content_score.get(c, 0.0) - lo) / (hi - lo) if hi > lo else 0.0
    W = {"res": 0.9, "st": 0.8, "fld": 0.7, "imp": 0.35}
    score = {}
    for c in allids:
        s = cn(c); sigs = 0
        if c in res: s += W["res"]; sigs += 1
        if c in st:  s += W["st"];  sigs += 1
        if c in fld: s += W["fld"]; sigs += 1
        if c in imp: s += W["imp"]; sigs += 1
        if c in co:  s += 0.6 * (co[c] / maxco); sigs += 1
        s += 0.25 * max(0, sigs - 1)                       # consensus bonus
        score[c] = s
    return score, pool


def main():
    paths = [p for p in glob.glob("eval/bugs/*.json") if not os.path.basename(p).startswith("_")]
    bugs = D.load_bugs(paths)
    def topk(score, k):
        return set(sorted(score, key=score.get, reverse=True)[:k])
    cfg = ["codefirst@8", "precise-union@8", "all-union@8",
           "codefirst@20", "precise-union@20", "all-union@20", "pool-ceiling"]
    rows = {c: [] for c in cfg}
    for bug in bugs:
        idx = cli.fresh_index(bug.repo_dir); rg = idx["rg"]
        gold = M.gold_symbols(rg, bug.gold)
        if not gold:
            continue
        nodes = CH.content_nodes(rg)
        content_score, _ = RK.rank_nodes(idx, bug.issue_text, bug.repo_dir, nodes, weights=SL.load_weights(bug.repo_dir))
        seeds = set(sorted(content_score, key=content_score.get, reverse=True)[:8])
        rp, _ = ranked_union(idx, bug.issue_text, bug.repo_dir, content_score, seeds, precise_only=True)
        ra, pool = ranked_union(idx, bug.issue_text, bug.repo_dir, content_score, seeds, precise_only=False)
        rec = lambda s: len(gold & s) / len(gold)
        rows["codefirst@8"].append(rec(topk(content_score, 8)))
        rows["precise-union@8"].append(rec(topk(rp, 8)))
        rows["all-union@8"].append(rec(topk(ra, 8)))
        rows["codefirst@20"].append(rec(topk(content_score, 20)))
        rows["precise-union@20"].append(rec(topk(rp, 20)))
        rows["all-union@20"].append(rec(topk(ra, 20)))
        rows["pool-ceiling"].append(rec(pool))
    n = len(rows["codefirst@8"])
    print(f"curated bugs measured: {n}\n")
    print(f"  {'config':20s} {'symbol-recall':>13s}")
    for c in cfg:
        print(f"  {c:20s} {sum(rows[c]) / n:13.2f}")
    print("\n  (pool-ceiling = recall if we returned the whole multi-signal union; the question is how"
          " much of it the ranker captures at fixed small k.)")


if __name__ == "__main__":
    main()
