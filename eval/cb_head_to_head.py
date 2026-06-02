#!/usr/bin/env python3
"""VARD arm of the ContextBench head-to-head, at scale. Loads a language subset, runs VARD localization
per instance (file/symbol recall + the token-cost of the context VARD hands over + ~0 LLM tokens), and
dumps per-instance issue/repo/gold so the agent arm can be run on a subset and compared.

  python -m eval.cb_head_to_head <split> <lang> [limit]
"""
import sys, os, json, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vard import cli
from eval import contextbench as CB, metric as M, retrievers as R


def toks(s):
    return len(s) // 4


def main():
    split = sys.argv[1] if len(sys.argv) > 1 else "contextbench_verified"
    lang = sys.argv[2] if len(sys.argv) > 2 else "java"
    limit = int(sys.argv[3]) if len(sys.argv) > 3 else None
    bugs = CB.load_cb_bugs(split=split, lang=lang, limit=limit)
    print(f"loaded {len(bugs)} {lang} bugs from {split}\n")
    agg = {"n": 0, "cf_f": 0, "gh_f": 0, "gf": 0, "cf_s": 0, "gh_s": 0, "gs": 0, "ctx_toks": 0, "qsec": 0.0}
    dump = []
    for bug in bugs:
        try:
            idx = cli.fresh_index(bug.repo_dir)
            if not idx:
                continue
        except Exception as e:
            print(f"  ! index fail {bug.id}: {str(e)[:50]}"); continue
        rg = idx["rg"]
        gfiles = sorted({s.file for s in bug.gold})
        gsyms = M.gold_symbols(rg, bug.gold)
        if not gsyms:
            continue
        t = time.time()
        cf = R.vard_codefirst(idx, bug.issue_text, bug.repo_dir, 8)
        gh = R.general_hybrid(idx, bug.issue_text, bug.repo_dir, 8)
        qsec = time.time() - t
        ctx = cli.context_text(bug.issue_text, bug.repo_dir, k=8)
        cff = len({rg.nodes[i].file for i in cf} & set(gfiles))
        ghf = len({rg.nodes[i].file for i in gh} & set(gfiles))
        agg["n"] += 1; agg["gf"] += len(gfiles); agg["gs"] += len(gsyms)
        agg["cf_f"] += cff; agg["gh_f"] += ghf
        agg["cf_s"] += len(cf & gsyms); agg["gh_s"] += len(gh & gsyms)
        agg["ctx_toks"] += toks(ctx); agg["qsec"] += qsec
        print(f"  {bug.id[:46]:46s} gold_files={len(gfiles)} cf={cff} gh={ghf}  ctx={toks(ctx)}tok")
        dump.append({"id": bug.id, "repo_dir": bug.repo_dir, "issue": bug.issue_text,
                     "gold_files": gfiles})
    a = agg
    print("\n==== VARD arm aggregate ====")
    print(f"  instances: {a['n']}")
    print(f"  file recall  codefirst@8: {a['cf_f']}/{a['gf']} ({100*a['cf_f']//max(a['gf'],1)}%)   "
          f"general_hybrid@8: {a['gh_f']}/{a['gf']} ({100*a['gh_f']//max(a['gf'],1)}%)")
    print(f"  symbol recall codefirst@8: {a['cf_s']}/{a['gs']}   general_hybrid@8: {a['gh_s']}/{a['gs']}")
    print(f"  avg context handed: {a['ctx_toks']//max(a['n'],1)} tokens   avg query: {a['qsec']/max(a['n'],1):.1f}s   LLM tokens: 0")
    json.dump(dump, open("/tmp/cb_agent_inputs.json", "w"))
    print(f"  dumped {len(dump)} instances for the agent arm -> /tmp/cb_agent_inputs.json")


if __name__ == "__main__":
    main()
