#!/usr/bin/env python3
"""Adaptive SEED selection sweep. Seeds (the anchors the pool expands from) were a fixed top-8; that
gates the expansion, so gold coupled only to a mid-ranked anchor gets orphaned (audit #15). Question:
can we pick the seed count at runtime from the score distribution instead? Measure each strategy's pool
CEILING (recall if you returned the pool) and pool SIZE across every bug, so we see lift vs cost.

  python -m eval.seed_sweep [manifest globs...]
"""
import sys, os, glob, collections
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vard import cli, rank as RK, selflabel as SL
from eval import dataset as D, metric as M, channels as CH, candidates as CAND

FLOOR, CAP = 5, 30


def _topk(order, score, k):
    return order[:k]


def _relative(order, score, alpha):
    top = score[order[0]] if order else 0.0
    seeds = [c for c in order[:CAP] if score[c] >= alpha * top]
    return seeds[:CAP] if len(seeds) >= FLOOR else order[:FLOOR]


def _mass(order, score, m):
    region = order[:CAP]
    lo = min((score[c] for c in region), default=0.0)
    shifted = [(c, max(0.0, score[c] - lo)) for c in region]      # shift so the weakest in-region ~ 0
    total = sum(w for _, w in shifted) or 1.0
    seeds, cum = [], 0.0
    for c, w in shifted:
        seeds.append(c); cum += w
        if cum / total >= m and len(seeds) >= FLOOR:
            break
    return seeds[:CAP]


def _knee(order, score):
    region = order[:CAP]
    if len(region) <= FLOOR:
        return region
    best_i, best_drop = len(region), -1.0
    for i in range(FLOOR, len(region) - 1):
        a, b = score[region[i]], score[region[i + 1]]
        drop = (a - b) / a if a > 0 else 0.0                      # relative gap
        if drop > best_drop:
            best_drop, best_i = drop, i + 1
    return region[:best_i]


def strategies(order, score):
    return {
        "top8 (current)": _topk(order, score, 8),
        "top15":          _topk(order, score, 15),
        "top20":          _topk(order, score, 20),
        "rel.0.5":        _relative(order, score, 0.5),
        "rel.0.35":       _relative(order, score, 0.35),
        "mass.85":        _mass(order, score, 0.85),
        "mass.9":         _mass(order, score, 0.9),
        "knee":           _knee(order, score),
    }


def main():
    paths = sys.argv[1:] if len(sys.argv) > 1 else \
        [p for p in glob.glob("eval/bugs/*.json") if not os.path.basename(p).startswith("_")]
    bugs = D.load_bugs(paths)
    agg = collections.defaultdict(list)   # strat -> [(ceiling, pool_size, n_seeds)]
    per_bug = {}
    for bug in bugs:
        idx = cli.fresh_index(bug.repo_dir); rg = idx["rg"]
        gold = M.gold_symbols(rg, bug.gold)
        if not gold:
            continue
        nodes = CH.content_nodes(rg)
        cs, _ = RK.rank_nodes(idx, bug.issue_text, bug.repo_dir, nodes, weights=SL.load_weights(bug.repo_dir))
        order = sorted(cs, key=cs.get, reverse=True)
        row = {}
        for name, seeds in strategies(order, cs).items():
            seeds = set(seeds)
            pool = set(CAND.candidate_pool(idx, bug.issue_text, bug.repo_dir, cs, seeds).keys())
            ceil = len(gold & pool) / len(gold)
            agg[name].append((ceil, len(pool), len(seeds)))
            row[name] = (ceil, len(pool), len(seeds))
        per_bug[bug.id] = row
        print(f"  {bug.id:30s} " + "  ".join(f"{n.split()[0]}={row[n][0]:.2f}" for n in row), flush=True)
    print(f"\n  {'strategy':16s} {'avg ceiling':>12s} {'avg pool':>9s} {'avg seeds':>10s} {'#bugs<1.0':>10s}")
    for name in strategies([], {}):
        rows = agg[name]
        if not rows:
            continue
        c = sum(r[0] for r in rows) / len(rows)
        p = sum(r[1] for r in rows) / len(rows)
        s = sum(r[2] for r in rows) / len(rows)
        below = sum(1 for r in rows if r[0] < 1.0)
        print(f"  {name:16s} {c:12.3f} {p:9.0f} {s:10.1f} {below:10d}")


if __name__ == "__main__":
    main()
