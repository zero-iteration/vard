#!/usr/bin/env python3
"""BUDGET-MATCHED comparison — the decisive test. Pool ceiling (1.00) vs codefirst@8 (0.18) is unfair:
the pool returns ~P symbols, codefirst returns 8. The honest question: at the SAME budget as the pool,
does the pool recover MORE gold than just returning that many content hits — or is its recall only because
it is bigger?

For each bug we take VARD's pool (size P symbols, L total lines) and evaluate three content-only baselines
AT THE POOL'S OWN BUDGET:
  - bm25        pure lexical
  - semantic    pure embeddings
  - codefirst   the shipped combined ranker (bm25+sem+history+PPR) — the STRONGEST content baseline
Two budgets: matched SYMBOL count (top-P) and matched TOKEN budget (greedy by score until >= L lines).
If POOL recall > codefirst at matched budget, the structural/coupling signals add real recall. If equal,
the pool is just "more content".

  python -m eval.budget_match [manifest globs...]
"""
import sys, os, glob, collections
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vard import cli, rank as RK, selflabel as SL
from eval import dataset as D, metric as M, channels as CH, candidates as CAND


def _topn(score, n):
    return set(sorted(score, key=score.get, reverse=True)[:n])


def _by_lines(score, rg, budget):
    """Greedy: take highest-scoring symbols until their cumulative line-count reaches `budget`."""
    out, lines = set(), 0
    for nid in sorted(score, key=score.get, reverse=True):
        n = rg.nodes.get(nid)
        if n is None:
            continue
        out.add(nid); lines += (n.end - n.start + 1)
        if lines >= budget:
            break
    return out


def main():
    args = sys.argv[1:]
    if args and args[0] == "cb":                      # cb <lang> [split] [limit]  -> ContextBench
        from eval import contextbench as CB
        lang = args[1] if len(args) > 1 else "java"
        split = args[2] if len(args) > 2 else "contextbench_verified"
        limit = int(args[3]) if len(args) > 3 else None
        print(f"  loading ContextBench: split={split} lang={lang} limit={limit}", flush=True)
        bugs = CB.load_cb_bugs(split=split, lang=lang, limit=limit)
    else:
        paths = args or [p for p in glob.glob("eval/bugs/*.json") if not os.path.basename(p).startswith("_")]
        bugs = D.load_bugs(paths)
    agg = collections.defaultdict(list)
    hard = collections.defaultdict(list)              # subset: content (codefirst) can't get all gold @ budget
    print(f"  {'bug':30s} {'P':>4s} | {'--- recall @ matched symbols ---':>34s} | {'-- recall @ matched tokens --':>30s}")
    print(f"  {'':30s} {'':>4s} | {'bm25':>6s} {'sem':>6s} {'cf':>6s} {'POOL':>7s} | {'bm25':>6s} {'sem':>6s} {'cf':>6s} {'POOL':>6s}")
    for bug in bugs:
        idx = cli.fresh_index(bug.repo_dir); rg = idx["rg"]
        gold = M.gold_symbols(rg, bug.gold)
        if not gold:
            continue
        nodes = CH.content_nodes(rg)
        chunks, keys, bm = CH.chunk_index(nodes, bug.repo_dir)
        bm25 = CH.lexical_scores(bug.issue_text, keys, bm)
        sem = CH.semantic_scores(bug.issue_text, nodes, keys, chunks, bug.repo_dir)
        cf, _ = RK.rank_nodes(idx, bug.issue_text, bug.repo_dir, nodes, weights=SL.load_weights(bug.repo_dir))
        seeds = _topn(cf, 8)
        pool = set(CAND.candidate_pool(idx, bug.issue_text, bug.repo_dir, cf, seeds).keys())
        P = len(pool)
        L = sum(rg.nodes[i].end - rg.nodes[i].start + 1 for i in pool if i in rg.nodes)
        rec = lambda s: len(gold & s) / len(gold)
        r_pool = rec(pool)
        row_sym = {"bm25": rec(_topn(bm25, P)), "sem": rec(_topn(sem, P)) if sem else 0.0,
                   "cf": rec(_topn(cf, P)), "POOL": r_pool}
        row_tok = {"bm25": rec(_by_lines(bm25, rg, L)), "sem": rec(_by_lines(sem, rg, L)) if sem else 0.0,
                   "cf": rec(_by_lines(cf, rg, L)), "POOL": r_pool}
        is_hard = row_sym["cf"] < 1.0                 # content can't fully solve it at matched budget
        for k, v in row_sym.items():
            agg[("sym", k)].append(v)
            if is_hard:
                hard[("sym", k)].append(v)
        for k, v in row_tok.items():
            agg[("tok", k)].append(v)
            if is_hard:
                hard[("tok", k)].append(v)
        print(f"  {bug.id:30s} {P:>4d}{'*' if is_hard else ' '}| {row_sym['bm25']:6.2f} {row_sym['sem']:6.2f} {row_sym['cf']:6.2f} "
              f"{row_sym['POOL']:7.2f} | {row_tok['bm25']:6.2f} {row_tok['sem']:6.2f} {row_tok['cf']:6.2f} "
              f"{row_tok['POOL']:6.2f}", flush=True)

    def report(d, label):
        n = len(d[("sym", "POOL")])
        if not n:
            print(f"\n  {label}: (no instances)"); return
        avg = lambda key: sum(d[key]) / len(d[key])
        print(f"  {label+' (n=%d)'%n:34s} | "
              f"{avg(('sym','bm25')):6.2f} {avg(('sym','sem')):6.2f} {avg(('sym','cf')):6.2f} {avg(('sym','POOL')):7.2f} | "
              f"{avg(('tok','bm25')):6.2f} {avg(('tok','sem')):6.2f} {avg(('tok','cf')):6.2f} {avg(('tok','POOL')):6.2f}")
    print()
    report(agg, "ALL")
    report(hard, "CONTENT-HARD subset")   # where codefirst < 1.0 @ budget = where structural signals can matter
    print("\n  * = content-hard (codefirst < 1.0 at matched budget). The verdict lives in that subset:")
    print("  POOL > cf there => structural signals recover gold content cannot, at equal budget.")


if __name__ == "__main__":
    main()
