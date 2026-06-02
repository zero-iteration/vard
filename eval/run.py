#!/usr/bin/env python3
"""Run the dark-gold eval.

  python -m eval.run eval/bugs/*.json                 # all retrievers, default k
  python -m eval.run --retriever coupling eval/bugs/x.json
  python -m eval.run --self-test                      # plumbing smoke test on VARD's own repo

Report: per-bug dark-gold count + each retriever's marginal recall on dark gold, then
aggregates split by bug_class (coupling vs logic). The coupling-class dark-gold recall is
the headline.
"""
import argparse, glob, os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root for `vard`

from vard import cli
from eval import dataset as D, metric as M, retrievers as R


def _index(repo):
    idx = cli.fresh_index(repo)
    if not idx:
        cli.build_index(repo)
        idx = cli.load_index(repo)
    return idx


def run(bug_paths, retriever_names, k_channel, k_ret):
    bugs = D.load_bugs(bug_paths)
    if not bugs:
        print("no bugs loaded — see eval/bugs/_TEMPLATE.json"); return
    rows = {name: [] for name in retriever_names}
    for bug in bugs:
        print(f"\n=== {bug.id}  [{bug.bug_class}]  {bug.repo_url}")
        idx = _index(bug.repo_dir)
        if not idx:
            print("  ! could not build index"); continue
        for name in retriever_names:
            got = R.REGISTRY[name](idx, bug.issue_text, bug.repo_dir, k=k_ret)
            res = M.evaluate(bug, idx, got, k_channel=k_channel)
            rows[name].append(res)
            print(f"  {name:10s}  gold_syms={res.n_gold_syms:2d}  dark={res.n_dark:2d}  "
                  f"(lex {res.reach_lex} sem {res.reach_sem} struct {res.reach_struct})  "
                  f"dark_recall={_pct(res.dark_recall)}  gold_recall={_pct(res.gold_recall)}")
    _summary(rows)


def _pct(x):
    return "  n/a" if x is None else f"{x*100:4.0f}%"


def _summary(rows):
    print("\n" + "=" * 64 + "\nSUMMARY (marginal recall on dark gold)\n" + "=" * 64)
    for name, rs in rows.items():
        for klass in ("coupling", "logic", "unknown", "ALL"):
            sub = rs if klass == "ALL" else [r for r in rs if r.bug_class == klass]
            withdark = [r for r in sub if r.n_dark]
            if not sub:
                continue
            dr = sum(r.retriever_dark_hits for r in withdark)
            dt = sum(r.n_dark for r in withdark)
            gr = sum(r.retriever_total_hits for r in sub)
            gt = sum(r.n_gold_syms for r in sub)
            dark_recall = f"{dr}/{dt} ({dr/dt*100:.0f}%)" if dt else "no dark gold"
            print(f"  {name:10s} {klass:9s} n={len(sub):2d}  dark_gold_recall={dark_recall:18s}  "
                  f"gold_recall={gr}/{gt} ({gr/gt*100:.0f}%)" if gt else "")


def self_test():
    """No curated data needed: index VARD's own repo, fire a synthetic query, prove the three
    channels + metric run end to end. NOT a result — just plumbing."""
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    print(f"self-test on {repo}")
    idx = _index(repo)
    rg = idx["rg"]
    nodes = M.CH.content_nodes(rg)
    print(f"  {len(nodes)} content nodes")
    task = "rank nodes by combining bm25 and embeddings into a fused score"
    chunks, keys, bm = M.CH.chunk_index(nodes, repo)
    lex = M.CH.topk_ids(M.CH.lexical_scores(task, keys, bm), 10)
    sem = M.CH.topk_ids(M.CH.semantic_scores(task, nodes, keys, chunks, repo), 10)
    reach = M.CH.structural_reach_files(idx, task, nodes)
    print(f"  lexical@10={len(lex)}  semantic@10={len(sem)}  structural_files={len(reach)}")
    got = R.vard_codefirst(idx, task, repo, k=8)
    print(f"  codefirst returned {len(got)} ids; sample: {sorted(got)[:3]}")
    print("  OK — plumbing works." if lex and got else "  ! something returned empty")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("bugs", nargs="*", help="manifest json paths or globs")
    ap.add_argument("--retriever", action="append", choices=list(R.REGISTRY),
                    help="restrict to these (default: all)")
    ap.add_argument("--k-channel", type=int, default=10, help="top-k that defines 'reachable' per channel")
    ap.add_argument("--k-ret", type=int, default=8, help="top-k the retriever returns")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        self_test(); return
    paths = [p for pat in a.bugs for p in glob.glob(pat)
             if not os.path.basename(p).startswith("_")]   # skip _TEMPLATE.json etc.
    if not paths:
        ap.error("no bug manifests matched; try eval/bugs/*.json or --self-test")
    run(paths, a.retriever or list(R.REGISTRY), a.k_channel, a.k_ret)


if __name__ == "__main__":
    main()
