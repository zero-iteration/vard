#!/usr/bin/env python3
"""Head-to-head: softmax/probability identifier vs LLM agent identifier, on the 3 dark-gold bugs.

Both name implicated STATE TYPES; VARD traverses each set; we measure whether the dark-gold's own
types were named and how much dark gold is recovered. Run in two phases:
  phase 1 (this script, mode=softmax): scores softmax picks + dumps blind agent inputs to /tmp/agent_inputs.json
  phase 2 (after the agent fills /tmp/identified.json via save_identified): mode=compare
"""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vard import cli
from eval import dataset as D, contextbench as CB, identify as ID, state_lineage as SLG, metric as M
import pyarrow.dataset as ds


def load_dark_bugs():
    bugs = []
    for b in ["kcloud-cache-result", "thrivex-oss-dynamic"]:
        bugs.append(D.load_bug(f"eval/bugs/{b}.json"))
    t = ds.dataset("/Users/zero_iteration/Desktop/vard-bench/contextbench/data/contextbench_verified.parquet",
                   format="parquet").to_table().to_pylist()
    r = [x for x in t if x.get("repo") == "mockito/mockito"][0]
    repo = "/Users/zero_iteration/.vard-eval/repos/mockito__mockito"
    CB._checkout_or_fetch(repo, r["base_commit"])
    gold = [D.Span(g["file"], int(g["start_line"]), int(g["end_line"])) for g in json.loads(r["gold_context"])]
    bugs.append(D.Bug(id="mockito", issue_text=r["problem_statement"], bug_class="cb", repo_dir=repo, gold=gold))
    return bugs


def dark_info(idx, bug):
    """Returns (dark_gold_symbol_ids, dark_state_type_names)."""
    rg = idx["rg"]
    g = M.gold_symbols(rg, bug.gold)
    nodes = SLG.CH.content_nodes(rg)
    chunks, keys, bm = SLG.CH.chunk_index(nodes, bug.repo_dir)
    lex = SLG.CH.topk_ids(SLG.CH.lexical_scores(bug.issue_text, keys, bm), 10)
    sem = SLG.CH.topk_ids(SLG.CH.semantic_scores(bug.issue_text, nodes, keys, chunks, bug.repo_dir), 10)
    sf = {rg.nodes[i].file for i in (lex | sem)}
    struct = {n.id for n in nodes if n.file in SLG.CH.structural_reach_files(idx, sf, 1)}
    conv = lex | sem | struct
    dark = g - conv
    dtypes = {rg.nodes[nid].qual.split("::")[-1].split(".")[0] for nid in dark}
    return dark, dtypes


def recover(idx, types, repo, dark_syms):
    sg = SLG.build_state_graph(idx, repo); rg = idx["rg"]
    clo = set()
    for t in types:
        if t in sg["type_def"]:
            clo |= SLG._type_closure(sg, t, rg)
    return len(dark_syms & clo), len(dark_syms), len(clo)


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "softmax"
    bugs = load_dark_bugs()
    agent_inputs = {}
    for bug in bugs:
        idx = cli.fresh_index(bug.repo_dir)
        dark, dtypes = dark_info(idx, bug)
        cands, _ = ID.candidate_types(idx, bug.repo_dir, bug.issue_text)
        print(f"\n=== {bug.id}  (dark gold: {len(dark)} syms; their types: {sorted(dtypes)})")
        # SOFTMAX side
        sm = ID.softmax_identify(idx, bug.issue_text, bug.repo_dir, topn=10)
        sm_named = sorted(set(sm) & dtypes)
        sm_rec = recover(idx, sm, bug.repo_dir, dark)
        print(f"  SOFTMAX(lex+sem+ppr) top10: {sm[:10]}")
        print(f"     named dark-state types: {sm_named or 'NONE'}   dark recovered: {sm_rec[0]}/{sm_rec[1]} (closure {sm_rec[2]})")
        # STRUCTURAL identifier (anchor on symptom-named operations -> def-use to state; no lexical/sem on state)
        st = ID.structural_identify(idx, bug.issue_text, bug.repo_dir, topn=12)
        st_named = sorted(set(st) & dtypes)
        st_rec = recover(idx, st, bug.repo_dir, dark)
        print(f"  STRUCTURAL top12: {st}")
        print(f"     named dark-state types: {st_named or 'NONE'}   dark recovered: {st_rec[0]}/{st_rec[1]} (closure {st_rec[2]})")
        # AGENT side
        if mode == "compare":
            ag = [t for t in ID.load_identified(bug.issue_text)]
            ag_named = sorted(set(ag) & dtypes)
            ag_rec = recover(idx, ag, bug.repo_dir, dark)
            print(f"  AGENT picks: {ag}")
            print(f"     named dark-state types: {ag_named or 'NONE'}   dark recovered: {ag_rec[0]}/{ag_rec[1]} (closure {ag_rec[2]})")
        agent_inputs[bug.id] = {"task_key": ID._key(bug.issue_text), "symptom": bug.issue_text[:1200],
                                "candidates": cands}
    if mode == "softmax":
        json.dump(agent_inputs, open("/tmp/agent_inputs.json", "w"))
        print(f"\n>> dumped {len(agent_inputs)} blind agent inputs to /tmp/agent_inputs.json")


if __name__ == "__main__":
    main()
