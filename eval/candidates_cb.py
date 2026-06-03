#!/usr/bin/env python3
"""Build provenance-tagged candidate pools for ContextBench instances across MANY repos/languages,
so we can test agent-from-pool on diverse real bug-fixes (not just the 8 hand-curated coupling bugs).

Dumps /tmp/cb_cand_pools.json (blind to gold, for agents) + /tmp/cb_cand_gold.json (gold + codefirst
baseline + ceiling, for scoring).  python -m eval.candidates_cb [langs] [per_lang]
"""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vard import cli, rank as RK, selflabel as SL
from eval import contextbench as CB, candidates as CAND, metric as M, channels as CH


def fileq(rg, nid):
    n = rg.nodes[nid]
    return n.file + "::" + n.qual.split("::")[-1]


def main():
    langs = (sys.argv[1].split(",") if len(sys.argv) > 1 else
             ["java", "go", "javascript", "typescript", "python"])
    per = int(sys.argv[2]) if len(sys.argv) > 2 else 4
    pools, meta = {}, {}
    for lang in langs:
        try:
            bugs = CB.load_cb_bugs(split="contextbench_verified", lang=lang, limit=per)
        except Exception as e:
            print(f"  ! {lang}: {str(e)[:60]}"); continue
        for bug in bugs:
            try:
                idx = cli.fresh_index(bug.repo_dir)
                if not idx:
                    continue
                rg = idx["rg"]
                gold = M.gold_symbols(rg, bug.gold)
                if not gold:
                    continue
                nodes = CH.content_nodes(rg)
                cs, _ = RK.rank_nodes(idx, bug.issue_text, bug.repo_dir, nodes, weights=SL.load_weights(bug.repo_dir))
                seeds = set(sorted(cs, key=cs.get, reverse=True)[:8])
                pool = CAND.candidate_pool(idx, bug.issue_text, bug.repo_dir, cs, seeds)
                cands = [{"id": f"{v['file']}::{v['qual']}", "qual": v["qual"], "file": v["file"],
                          "lines": v["lines"], "tags": v["tags"]} for v in pool.values()]
                gq = sorted({fileq(rg, g) for g in gold})
                cf8 = sorted({fileq(rg, c) for c in seeds})
                poolids = {c["id"] for c in cands}
                bid = f"{lang}-{bug.id[-10:]}"
                pools[bid] = {"issue": bug.issue_text[:900], "candidates": cands}
                meta[bid] = {"gold": gq, "cf8": cf8, "lang": lang,
                             "ceiling": len(set(gq) & poolids) / len(gq) if gq else 0.0}
                cff = len({x.split('::')[0] for x in cf8} & {x.split('::')[0] for x in gq}) / len({x.split('::')[0] for x in gq})
                print(f"  {bid:26s} pool={len(cands):4d} gold={len(gq):2d} cf@8(file)={cff:.2f} ceil={meta[bid]['ceiling']:.2f}", flush=True)
            except Exception as e:
                print(f"  ! {bug.id[-10:]}: {str(e)[:60]}", flush=True)
    json.dump(pools, open("/tmp/cb_cand_pools.json", "w"))
    json.dump(meta, open("/tmp/cb_cand_gold.json", "w"))
    print(f"\ndumped {len(meta)} pools across {len(set(m['lang'] for m in meta.values()))} languages -> /tmp/cb_cand_pools.json")


if __name__ == "__main__":
    main()
