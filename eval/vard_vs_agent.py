#!/usr/bin/env python3
"""VARD vs a plain agent, head to head, on a real bug at its pre-fix commit.

VARD arm (this script): deterministic local retrieval — localization recall, the context it hands
the agent (token count), query latency, LLM-token cost (~0, local embeddings). The agent arm is run
separately as a subagent (its tokens / tool-calls / latency come from the Agent tool result).
"""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vard import cli
from eval import dataset as D, metric as M, retrievers as R, channels as CH


def toks(s):
    return len(s) // 4                      # rough token estimate


def main():
    bug = D.load_bug("eval/bugs/youlai-currentuser.json")
    print(f"repo: {bug.repo_url}  base: {bug.base_commit}  fix: {bug.fix_commit}")
    print(f"ISSUE: {bug.issue_text}\n")
    t0 = time.time()
    idx = cli.fresh_index(bug.repo_dir)
    t_index = time.time() - t0
    rg = idx["rg"]
    gfiles = sorted({s.file for s in bug.gold})
    gsyms = M.gold_symbols(rg, bug.gold)
    print(f"GOLD files: {gfiles}")
    print(f"index built in {t_index:.1f}s  ({len(rg.nodes)} symbols, {len({n.file for n in rg.nodes.values()})} files)\n")

    # VARD arm: localization + the context it would hand the agent
    t1 = time.time()
    ctx = cli.context_text(bug.issue_text, bug.repo_dir, k=8)
    t_query = time.time() - t1
    nodes = CH.content_nodes(rg)
    for label, k in [("hybrid", 8), ("hybrid", 20)]:
        got = R.hybrid(idx, bug.issue_text, bug.repo_dir, k=k)
        gotfiles = {rg.nodes[i].file for i in got}
        frec = len(set(gfiles) & gotfiles) / len(gfiles) if gfiles else 0
        srec = len(gsyms & got) / len(gsyms) if gsyms else 0
        print(f"  VARD {label}@{k}: file_recall={frec:.0%} ({len(set(gfiles)&gotfiles)}/{len(gfiles)})  "
              f"symbol_recall={srec:.0%}  output_spans={len(got)}")
    print(f"\n  VARD query latency: {t_query:.2f}s   context handed to agent: {toks(ctx)} tokens   "
          f"LLM tokens used: 0 (local bge-small embeddings)")
    print("\n--- VARD context (what it feeds the agent instead of it searching) ---")
    print(ctx[:1400])


if __name__ == "__main__":
    main()
