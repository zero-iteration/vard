#!/usr/bin/env python3
"""Test two coupling-retrieval ideas against the CURRENT behavior, on real repos.

Idea 1 — RWR (Random Walk with Restart) over a weighted entity-entity coupling graph (the bipartite
         entity-resource structure PROJECTED to file-file, weighted, then walked from the seed).
Idea 2 — Bipartite / inverted-index: keep the resource first-class, never project; score candidate
         files by shared resources weighted by mode x mutability x IDF(resource), aggregated noisy-OR.
Baseline — CURRENT state: VARD's coupling partners ranked by raw count of shared resources (unweighted).

All three consume the SAME edges VARD already extracts (idx["res"]) — so this isolates the retrieval
layer, not edge construction. match_confidence (AST key-resolution) is held at 1.0 (orthogonal).

Ground truth = git CO-CHANGE (files changed together in history), via vard.memory.mine_changes — used
only as held-out VALIDATION (per the design note), never as an input. It is a confounded proxy ("what
changes together"); read the numbers as relative, not absolute.

  python -m eval.coupling_compare <repo>
"""
import sys, os, math, collections
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np
from vard import cli, memory as MEM

MUT = {"table": 1.0, "queue": 0.6, "cache": 0.5}


def _mut(rid):
    return MUT.get(rid.split(":", 1)[0], 0.5)


def _mode_factor(ma, mb):
    if ("w" in ma and "r" in mb) or ("r" in ma and "w" in mb):
        return 1.0                                   # producer<->consumer: the real change-dependency
    if "w" in ma and "w" in mb:
        return 0.8                                   # write/write: ordering/invariants
    return 0.2                                       # read/read: weak (share a schema, neither breaks the other)


def resource_index(idx):
    """resource -> {file: {modes}} from VARD's existing resource layer."""
    rg = idx["rg"]
    res = idx.get("res") or {}
    R = collections.defaultdict(lambda: collections.defaultdict(set))
    for rid in res.get("nodes", []):
        for nid in res.get("writers", {}).get(rid, []):
            if nid in rg.nodes:
                R[rid][rg.nodes[nid].file].add("w")
        for nid in res.get("readers", {}).get(rid, []):
            if nid in rg.nodes:
                R[rid][rg.nodes[nid].file].add("r")
    # drop resources touched by a single file (no coupling) and module-level noise
    return {rid: dict(fm) for rid, fm in R.items() if len(fm) >= 2}


def file_resources(R):
    fr = collections.defaultdict(set)
    for rid, fm in R.items():
        for fl in fm:
            fr[fl].add(rid)
    return fr


def idf_map(R, n_files):
    return {rid: math.log(max(n_files, 2) / max(len(fm), 1)) for rid, fm in R.items()}


# ---- baseline (CURRENT): unweighted shared-resource count ----
def baseline_rank(R, fr, target):
    c = collections.Counter()
    for rid in fr.get(target, ()):
        for fl in R[rid]:
            if fl != target:
                c[fl] += 1
    return dict(c)


# ---- idea 2: bipartite / inverted-index (mode x mutability x IDF, noisy-OR) ----
def bipartite_rank(R, fr, idf, target):
    maxidf = max(idf.values()) if idf else 1.0
    parts = collections.defaultdict(list)
    for rid in fr.get(target, ()):
        tmode = R[rid][target]
        w_res = _mut(rid) * (idf[rid] / maxidf)
        for fl, modes in R[rid].items():
            if fl == target:
                continue
            parts[fl].append(min(max(_mode_factor(tmode, modes) * w_res, 0.0), 1.0))
    return {fl: 1.0 - float(np.prod([1 - w for w in ws])) for fl, ws in parts.items()}


# ---- idea 1: RWR over the projected weighted file-file graph ----
def rwr_rank(R, fr, idf, target, r=0.4, iters=80):
    files = sorted(fr)
    if target not in files or len(files) < 2:
        return {}
    idxm = {f: i for i, f in enumerate(files)}
    n = len(files)
    A = np.zeros((n, n), dtype=float)
    maxidf = max(idf.values()) if idf else 1.0
    for rid, fm in R.items():
        w_res = _mut(rid) * (idf[rid] / maxidf)
        items = list(fm.items())
        for i in range(len(items)):
            fa, ma = items[i]
            for j in range(i + 1, len(items)):
                fb, mb = items[j]
                w = _mode_factor(ma, mb) * w_res
                A[idxm[fa], idxm[fb]] += w
                A[idxm[fb], idxm[fa]] += w
    col = A.sum(0)
    col[col == 0] = 1.0
    S = A / col
    p0 = np.zeros(n)
    p0[idxm[target]] = 1.0
    p = p0.copy()
    for _ in range(iters):
        p = (1 - r) * S.dot(p) + r * p0
    return {files[i]: float(p[i]) for i in range(n) if files[i] != target}


def random_rank(R, fr, target):
    # deterministic pseudo-random score over the candidate pool — exposes volume/coverage effects
    return {f: (hash((target, f)) % 10_000) / 10_000.0 for f in fr if f != target}


def _recall_mrr(ranked, gold, ks=(5, 10)):
    order = [f for f, _ in sorted(ranked.items(), key=lambda kv: -kv[1])]
    pos = {f: i for i, f in enumerate(order)}
    out = {}
    for k in ks:
        topk = set(order[:k])
        out[f"r@{k}"] = len(topk & gold) / len(gold) if gold else 0.0
    ranks = [pos[g] + 1 for g in gold if g in pos]
    out["mrr"] = (1.0 / min(ranks)) if ranks else 0.0
    out["covered"] = len([g for g in gold if g in pos]) / len(gold) if gold else 0.0
    return out


def main():
    repo = cli._project_root(sys.argv[1]) if len(sys.argv) > 1 else "."
    idx = cli.fresh_index(repo)
    rg = idx["rg"]
    n_files = len({n.file for n in rg.nodes.values()})
    R = resource_index(idx)
    fr = file_resources(R)
    idf = idf_map(R, n_files)
    mem = MEM.mine_changes(repo)
    print(f"repo={os.path.basename(repo)}  files={n_files}  coupling-resources={len(R)}  "
          f"resource-touching files={len(fr)}")

    methods = {"baseline(current)": baseline_rank, "bipartite": bipartite_rank, "rwr": rwr_rank,
               "random(pool)": random_rank}
    agg = {m: collections.defaultdict(list) for m in methods}
    n_targets = 0
    for target in fr:
        # held-out ground truth: files that co-changed with target >=2x AND are in the candidate space
        co = mem["cochange"].get(target, {})
        gold = {f for f, c in co.items() if c >= 2 and f in fr and f != target}
        if len(gold) < 2:
            continue
        n_targets += 1
        for m, fn in methods.items():
            ranked = fn(R, fr, target) if m in ("baseline(current)", "random(pool)") else fn(R, fr, idf, target)
            for kk, vv in _recall_mrr(ranked, gold).items():
                agg[m][kk].append(vv)
    print(f"targets evaluated (resource-touching, >=2 co-changed partners): {n_targets}\n")
    print(f"{'method':20s} {'recall@5':>9s} {'recall@10':>10s} {'MRR':>6s} {'coverage':>9s}")
    for m in methods:
        a = agg[m]
        def mean(k): return sum(a[k]) / len(a[k]) if a[k] else 0.0
        print(f"{m:20s} {mean('r@5'):9.3f} {mean('r@10'):10.3f} {mean('mrr'):6.3f} {mean('covered'):9.3f}")


if __name__ == "__main__":
    main()
