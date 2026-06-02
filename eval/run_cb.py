#!/usr/bin/env python3
"""Run codefirst vs hybrid vs state_lineage on the ContextBench Java subset (trusted external data).
  python -m eval.run_cb [split] [limit]
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vard import cli
from eval import contextbench as CB, metric as M, retrievers as R

RETR = ["codefirst", "hybrid", "state_lineage"]


def main():
    split = sys.argv[1] if len(sys.argv) > 1 else "contextbench_verified"
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else None
    bugs = CB.load_cb_bugs(split=split, lang="java", limit=limit)
    print(f"\nloaded {len(bugs)} ContextBench java bugs from {split}")
    agg = {r: {"dark_hit": 0, "dark_tot": 0, "gold_hit": 0, "gold_tot": 0, "fired": 0} for r in RETR}
    for bug in bugs:
        try:
            idx = cli.fresh_index(bug.repo_dir)
            if not idx:
                print(f"  ! no index {bug.id}"); continue
        except Exception as e:
            print(f"  ! index fail {bug.id}: {str(e)[:60]}"); continue
        line = f"  {bug.id:50s}"
        for name in RETR:
            got = R.REGISTRY[name](idx, bug.issue_text, bug.repo_dir, k=8)
            res = M.evaluate(bug, idx, got, k_channel=10)
            a = agg[name]
            a["dark_hit"] += res.retriever_dark_hits; a["dark_tot"] += res.n_dark
            a["gold_hit"] += res.retriever_total_hits; a["gold_tot"] += res.n_gold_syms
            if name == "hybrid" and len(got) > 8:
                a["fired"] += 1
            line += f"  {name[:4]}:d{res.n_dark}/{res.retriever_dark_hits}"
        print(line)
    print("\n==== ContextBench java summary ====")
    for name in RETR:
        a = agg[name]
        dr = f"{a['dark_hit']}/{a['dark_tot']}" if a["dark_tot"] else "no dark"
        print(f"  {name:13s} dark_gold_recall={dr:8s}  gold_recall={a['gold_hit']}/{a['gold_tot']}"
              + (f"  (state fired on {a['fired']} bugs)" if name == "hybrid" else ""))


if __name__ == "__main__":
    main()
