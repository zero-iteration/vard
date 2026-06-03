#!/usr/bin/env python3
"""Diagnose the POOL CEILING toward 95% recall. For each bug: which gold is in the current pool, and
which ADDITIONAL signal would catch the escapees — or whether gold is unreachable by ANY signal (the
hard floor = fix-introduced / structurally-disconnected code no retrieval can reach).

  python -m eval.ceiling_analysis [manifest globs...]
"""
import sys, os, glob, collections
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vard import cli, rank as RK, selflabel as SL, propagate as P
from eval import dataset as D, metric as M, channels as CH, candidates as CAND, recall_union as RU


def import_nhop(idx, seed_files, hops):
    adj = P.undirected_adj(idx.get("import_edges") or [])
    reach, fr = set(seed_files), set(seed_files)
    for _ in range(hops):
        nxt = set()
        for f in fr:
            nxt |= adj.get(f, set())
        nxt -= reach; reach |= nxt; fr = nxt
    return reach


def nodes_in_files(rg, files):
    return {n.id for n in CH.content_nodes(rg) if n.file in files}


def callgraph_1hop(idx, seeds):
    rg = idx["rg"]
    name2nodes = collections.defaultdict(set)
    for n in CH.content_nodes(rg):
        name2nodes[n.name].add(n.id)
    caller_calls = collections.defaultdict(set)   # caller node id -> callee method names
    for cs in getattr(rg, "call_sites", []):
        cid = f"{cs.file}::{cs.enclosing_qual}"
        if cid in rg.nodes:
            caller_calls[cid].add((cs.method or "").split(".")[-1])
    seed_names = {rg.nodes[s].name for s in seeds}
    out = set()
    for s in seeds:                               # callees of seeds
        for m in caller_calls.get(s, ()):
            out |= name2nodes.get(m, set())
    for cid, methods in caller_calls.items():     # callers of seeds
        if methods & seed_names:
            out.add(cid)
    return out


def pkg_siblings(rg, seed_files):
    dirs = {os.path.dirname(f) for f in seed_files}
    return {n.id for n in CH.content_nodes(rg) if os.path.dirname(n.file) in dirs}


def inheritance(idx, seeds):
    rg = idx["rg"]
    out = set()
    for s in seeds:
        out |= {v for _, v, k in rg.G.out_edges(s, keys=True) if k == "inherits"}
        out |= {u for u, _, k in rg.G.in_edges(s, keys=True) if k == "inherits"}
    return out


def all_cochange(idx, repo, seeds):
    from vard import memory as MEM
    rg = idx["rg"]; mem = MEM.mine_changes(repo)
    sfiles = {rg.nodes[i].file for i in seeds}
    cof = set()
    for f in sfiles:
        cof |= set(mem["cochange"].get(f, {}))
    return nodes_in_files(rg, cof)


def main():
    paths = sys.argv[1:] if len(sys.argv) > 1 else \
        [p for p in glob.glob("eval/bugs/*.json") if not os.path.basename(p).startswith("_")]
    bugs = D.load_bugs(paths)
    agg = collections.defaultdict(list)
    for bug in bugs:
        idx = cli.fresh_index(bug.repo_dir); rg = idx["rg"]
        gold = M.gold_symbols(rg, bug.gold)
        if not gold:
            continue
        nodes = CH.content_nodes(rg)
        cs, _ = RK.rank_nodes(idx, bug.issue_text, bug.repo_dir, nodes, weights=SL.load_weights(bug.repo_dir))
        seeds = set(sorted(cs, key=cs.get, reverse=True)[:8])
        sfiles = {rg.nodes[i].file for i in seeds}
        base = set(CAND.candidate_pool(idx, bug.issue_text, bug.repo_dir, cs, seeds).keys())
        exp = {
            "imp2": nodes_in_files(rg, import_nhop(idx, sfiles, 2)),
            "imp3": nodes_in_files(rg, import_nhop(idx, sfiles, 3)),
            "call": callgraph_1hop(idx, seeds),
            "pkg": pkg_siblings(rg, sfiles),
            "inh": inheritance(idx, seeds),
            "co_all": all_cochange(idx, bug.repo_dir, seeds),
        }
        allexp = base | set().union(*exp.values())
        rec = lambda s: len(gold & s) / len(gold)
        agg["base"].append((rec(base), len(base)))
        for k, v in exp.items():
            agg[k].append((rec(base | v), len(base | v)))
        agg["ALL"].append((rec(allexp), len(allexp)))
        unreached = gold - allexp
        agg["_unreached"].append(len(unreached) / len(gold))
        print(f"  {bug.id:26s} gold={len(gold):2d} base_ceil={rec(base):.2f}({len(base)}) "
              f"ALL_ceil={rec(allexp):.2f}({len(allexp)}) unreachable={len(unreached)}")
    n = len(agg["base"])
    print(f"\n  {'source':10s} {'ceiling':>8s} {'avg pool':>9s}")
    for k in ["base", "imp2", "imp3", "call", "pkg", "inh", "co_all", "ALL"]:
        c = sum(x[0] for x in agg[k]) / n
        sz = sum(x[1] for x in agg[k]) / n
        print(f"  {k:10s} {c:8.2f} {sz:9.0f}")
    print(f"\n  HARD FLOOR: avg gold unreachable by ANY signal = {sum(agg['_unreached'])/n:.0%} "
          f"(=> max achievable recall = {1-sum(agg['_unreached'])/n:.0%})")


if __name__ == "__main__":
    main()
