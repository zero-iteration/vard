#!/usr/bin/env python3
"""Rethink node/edge CONSTRUCTION, then re-test (new module; nothing in vard/ touched).

The Run-16 verdict: the retrieval algorithm (RWR/bipartite) is second-order; the bottleneck is that
detected coupling explains only ~10% of co-change (sparse edges). So change what nodes/edges exist.

v1 (current): resources = cache-keys / queue-topics / DB-tables, from call-pattern heuristics. Sparse.

v2 (here): generalize "resource" to ALL shared state — the densest of which is DOMAIN TYPES/ENTITIES.
A type is a resource; a file that PRODUCES it (constructs/builds/returns/mutates — from the state
graph's producers) is a writer; a file that REFERENCES it is a reader. IDF over how many files touch
the type is the native hub-correction (a type used by 200 files couples weakly; by 2, strongly). We
also keep the v1 cache/queue/table resources. Edges are mode-aware (producer↔consumer strongest).

Then we run the SAME comparison harness (baseline / bipartite / RWR / random vs git co-change) on v1
vs v2, to see if richer construction (a) raises COVERAGE (the ~10% ceiling) and (b) lets a principled
ranker finally beat random.

  python -m eval.edges2 <repo>
"""
import sys, os, collections
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vard import cli, state as ST, memory as MEM
from eval import coupling_compare as CC


_DATA_DIR = ("/model/", "/models/", "/dto/", "/entity/", "/entities/", "/vo/", "/bo/", "/co/",
             "/domain/", "/pojo/", "/payload/", "/event/", "/events/", "/record/")


def _is_data_type(sg, rg, t):
    if ST._DATA_LIKE.search(t):
        return True
    for did in sg["type_def"].get(t, []):
        f = rg.nodes[did].file.lower()
        if any(s in f for s in _DATA_DIR):
            return True
    return False


def resource_index_v2(idx, repo, max_site_frac=0.5, max_sites_abs=None, data_only=False):
    """resource -> {file: {modes}}. Generalizes v1 with TYPE/ENTITY resources from the state graph.
    data_only: only data-like/entity types (the actual shared STATE, not every class).
    max_sites_abs: hard low-fanout cap — a coupling type should be touched by FEW files."""
    rg = idx["rg"]
    sg = idx.get("state") or ST.build_state_graph(rg, os.path.abspath(repo))
    nfiles = len({n.file for n in rg.nodes.values()})
    R = collections.defaultdict(lambda: collections.defaultdict(set))

    for rid, fm in CC.resource_index(idx).items():       # keep v1 cache/queue/table resources
        for fl, modes in fm.items():
            R[rid][fl] |= modes

    f = lambda nid: rg.nodes[nid].file if nid in rg.nodes else None
    prod = sg.get("producers", {})
    for t, refids in sg["type_refs"].items():
        if ST._INFRA.search(t):
            continue
        if data_only and not _is_data_type(sg, rg, t):
            continue
        files_w = {f(n) for n in prod.get(t, []) if f(n)}
        files_r = {f(n) for n in refids if f(n)}
        allf = files_w | files_r
        if len(allf) < 2 or len(allf) > max_site_frac * max(nfiles, 1):
            continue
        if max_sites_abs and len(allf) > max_sites_abs:  # low-fanout only (specific shared state)
            continue
        rid = f"type:{t}"
        for fl in files_w:
            R[rid][fl].add("w")
        for fl in files_r - files_w:
            R[rid][fl].add("r")
    return {rid: dict(fm) for rid, fm in R.items() if len(fm) >= 2}


def _eval(R, repo, label):
    fr = CC.file_resources(R)
    n_files_total = len({n.file for n in cli.load_index(repo)["rg"].nodes.values()})
    idf = CC.idf_map(R, n_files_total)
    mem = MEM.mine_changes(repo)
    methods = {"baseline": CC.baseline_rank, "bipartite": CC.bipartite_rank,
               "rwr": CC.rwr_rank, "random": CC.random_rank}
    agg = {m: collections.defaultdict(list) for m in methods}
    n = 0
    for target in fr:
        co = mem["cochange"].get(target, {})
        gold = {x for x, c in co.items() if c >= 2 and x in fr and x != target}
        if len(gold) < 2:
            continue
        n += 1
        for m, fn in methods.items():
            ranked = fn(R, fr, target) if m in ("baseline", "random") else fn(R, fr, idf, target)
            for kk, vv in CC._recall_mrr(ranked, gold).items():
                agg[m][kk].append(vv)
    print(f"\n[{label}]  coupling-resources={len(R)}  resource-touching files={len(fr)}  targets={n}")
    print(f"  {'method':12s} {'r@10':>6s} {'MRR':>6s} {'coverage':>9s}")
    for m in methods:
        a = agg[m]
        mean = lambda k: sum(a[k]) / len(a[k]) if a[k] else 0.0
        print(f"  {m:12s} {mean('r@10'):6.3f} {mean('mrr'):6.3f} {mean('covered'):9.3f}")
    return agg


def main():
    repo = cli._project_root(sys.argv[1]) if len(sys.argv) > 1 else "."
    idx = cli.fresh_index(repo)
    print(f"repo={os.path.basename(repo)}")
    _eval(CC.resource_index(idx), repo, "v1  (current: cache/queue/table only)")
    _eval(resource_index_v2(idx, repo), repo, "v2  (+ ALL domain types)")
    _eval(resource_index_v2(idx, repo, data_only=True, max_sites_abs=15), repo,
          "v2-tight  (data/entity types only, low-fanout <=15 files)")


if __name__ == "__main__":
    main()
