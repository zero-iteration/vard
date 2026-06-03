#!/usr/bin/env python3
"""THE token-efficiency test (the actual product thesis): recall at SMALL fixed token budgets.

Earlier budget-matching used the POOL's size (huge) -> everything saturates and ties. The real claim is
"find the gold in FEWER tokens", so fix small budgets (1k/2k/5k/10k tokens) and compare recall. VARD's
output here is its REAL shipped small context, NOT the pool and NOT pure ranking:
    VARD = state-lineage spans (injected first, score-INDEPENDENT) ++ top content by score
This is the only way dark gold (low score by definition) can enter a small context. Baselines are pure
ranked retrieval: bm25 / semantic / codefirst (the combined content ranker).

  python -m eval.token_budget cb <split> [langs]      # e.g. cb full  (all supported langs)
  python -m eval.token_budget <manifest globs...>
"""
import sys, os, glob, collections
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vard import cli, rank as RK, selflabel as SL, state as ST
from eval import dataset as D, metric as M, channels as CH, recall_union as RU

BUDGETS = [2000, 5000, 10000, 25000, 50000, 100000]
SUPPORTED = ["java", "go", "javascript", "typescript", "python"]   # smaller-repo langs first; python giants last


def _toklen(repo, n, cache):
    return max(1, len(ST._node_text(repo, n, cache)) // 4)          # ~4 chars/token


def recall_at(order, gold, budget, repo, rg, cache):
    toks, taken = 0, set()
    for nid in order:
        n = rg.nodes.get(nid)
        if n is None:
            continue
        toks += _toklen(repo, n, cache)
        taken.add(nid)
        if toks >= budget:
            break
    return len(gold & taken) / len(gold)


def tokens_to_full(order, gold, repo, rg, cache):
    """Cumulative tokens needed before the ranked list has covered EVERY gold symbol."""
    toks, seen = 0, set()
    for nid in order:
        n = rg.nodes.get(nid)
        if n is None:
            continue
        toks += _toklen(repo, n, cache)
        if nid in gold:
            seen.add(nid)
            if seen >= gold:
                return toks
    return toks


def load(args):
    if args and args[0] == "cb":
        from eval import contextbench as CB
        split = args[1] if len(args) > 1 else "full"
        langs = (args[2].split(",") if len(args) > 2 else SUPPORTED)
        bugs = []
        for lg in langs:
            try:
                b = CB.load_cb_bugs(split=split, lang=lg)
                print(f"  loaded {len(b):4d} {lg} (of split={split})", flush=True)
                bugs += b
            except Exception as e:
                print(f"  ! {lg}: {str(e)[:70]}", flush=True)
        return bugs
    paths = args or [p for p in glob.glob("eval/bugs/*.json") if not os.path.basename(p).startswith("_")]
    return D.load_bugs(paths)


def main():
    bugs = load(sys.argv[1:])
    agg = {m: {b: [] for b in BUDGETS} for m in ("bm25", "sem", "cf", "VARD")}
    hard = {m: {b: [] for b in BUDGETS} for m in ("bm25", "sem", "cf", "VARD")}
    ratios, hard_ratios = [], []
    ran = 0

    def table(d, label):
        n = len(d["cf"][BUDGETS[0]])
        if not n:
            print(f"  {label}: (no instances)"); return
        print(f"  {label} (n={n})   recall @ token budget")
        print(f"  {'method':8s} " + " ".join(f"{b//1000}k".rjust(7) for b in BUDGETS))
        for m in ("bm25", "sem", "cf", "VARD"):
            print(f"  {m:8s} " + " ".join(f"{sum(d[m][b])/len(d[m][b]):7.3f}" for b in BUDGETS))

    def med(xs):
        xs = sorted(xs)
        return xs[len(xs) // 2] if xs else 0.0

    def report(tag):
        print(f"\n===== {tag}: scored {ran} of {len(bugs)} loaded =====", flush=True)
        table(agg, "ALL")
        table(hard, "CONTENT-HARD (codefirst<1.0 @100k)")
        print(f"  tokens-to-FULL ratio (content/VARD) median: ALL={med(ratios):.2f}x  HARD={med(hard_ratios):.2f}x",
              flush=True)

    for bug in bugs:
        try:
            idx = cli.fresh_index(bug.repo_dir); rg = idx["rg"]
            gold = M.gold_symbols(rg, bug.gold)
            if not gold:
                continue
            nodes = CH.content_nodes(rg)
            chunks, keys, bm = CH.chunk_index(nodes, bug.repo_dir)
            bm25 = CH.lexical_scores(bug.issue_text, keys, bm)
            sem = CH.semantic_scores(bug.issue_text, nodes, keys, chunks, bug.repo_dir)
            cf, _ = RK.rank_nodes(idx, bug.issue_text, bug.repo_dir, nodes, weights=SL.load_weights(bug.repo_dir))
            order = lambda s: sorted(s, key=s.get, reverse=True)
            cf_order = order(cf)
            seeds = set(cf_order[:8])
            sg = idx.get("state") or ST.build_state_graph(rg, bug.repo_dir)
            # VARD's small context injects its PRECISE signals ahead of content, score-INDEPENDENT, in
            # provenance-strength order (resource-coupled > state-lineage > field-flow) — this is how dark
            # gold (low content score) reaches a small context at all. Then content fills the rest.
            res = [i for i in RU._resource_partners(idx, seeds) if i in rg.nodes]
            stl = [i for i in ST.lineage(sg, rg, ST.auto_implicated(sg, rg, bug.issue_text, seeds)) if i in rg.nodes]
            fld = [i for i in RU._field_partners(idx, bug.repo_dir, seeds) if i in rg.nodes]
            inj, seen = [], set()
            for grp in (res, stl, fld):
                for i in grp:
                    if i not in seen:
                        seen.add(i); inj.append(i)
            vard_order = inj + [c for c in cf_order if c not in seen]
            orders = {"bm25": order(bm25), "sem": order(sem) if sem else [], "cf": cf_order, "VARD": vard_order}
            cache = {}
            ran += 1
            cf_full = recall_at(cf_order, gold, BUDGETS[-1], bug.repo_dir, rg, cache)
            is_hard = cf_full < 1.0
            for m, od in orders.items():
                for b in BUDGETS:
                    r = recall_at(od, gold, b, bug.repo_dir, rg, cache) if od else 0.0
                    agg[m][b].append(r)
                    if is_hard:
                        hard[m][b].append(r)
            ttf_cf = tokens_to_full(cf_order, gold, bug.repo_dir, rg, cache)
            ttf_vd = tokens_to_full(vard_order, gold, bug.repo_dir, rg, cache)
            ratio = ttf_cf / max(1, ttf_vd)        # >1 => content needs that many x more tokens for full recall
            ratios.append(ratio)
            if is_hard:
                hard_ratios.append(ratio)
            if ran % 5 == 0:
                report(f"checkpoint @ {ran}")
        except Exception as e:
            print(f"  ! {bug.id[-12:]}: {str(e)[:60]}", flush=True)

    report("FINAL")
    print("  VARD = precise coupling/state spans injected + content. Higher recall@budget / ratio>1 => fewer tokens.")


if __name__ == "__main__":
    main()
