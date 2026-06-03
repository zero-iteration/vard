#!/usr/bin/env python3
"""vard_candidates: the recall-complete, PROVENANCE-TAGGED candidate pool for the AGENT to rank.

Heuristic ranking of this pool fails (Runs 16-20). So VARD assembles the pool (each candidate tagged with
WHY it's there: content / resource-coupled / state-producer / field-flow / import-1hop / co-changed×N) and
the agent selects. This module: (1) builds + dumps the pool per curated bug (blind to gold) to
/tmp/cand_pools.json, (2) records gold + codefirst baseline + pool ceiling to /tmp/cand_gold.json.
Then agents select; eval/candidates_score.py scores agent-selection recall vs codefirst.

  python -m eval.candidates        # build + dump pools + baselines
"""
import sys, os, glob, json, collections
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vard import cli, rank as RK, selflabel as SL, state as ST
from vard.languages import profiles
from eval import dataset as D, metric as M, channels as CH, recall_union as RU


def candidate_pool(idx, task, repo, content_score, seeds, content_n=30):
    """Recall-complete, provenance-tagged pool the AGENT ranks. INVARIANT (see eval/FINDINGS Run 21):
    the pool layer TAGS and WEIGHTS, it never silently DROPS a candidate. So recovery signals
    (import-1hop, co-change, package-siblings) are NOT truncated by content score — that would re-drop the
    content-dark gold they exist to catch — and every count threshold is scale-relative, not a bare magic
    number. Truncation for precision is the agent's job, downstream."""
    rg = idx["rg"]
    nodes_all = CH.content_nodes(rg)
    total = max(1, len(nodes_all))
    sg = idx.get("state") or ST.build_state_graph(rg, repo)
    res = RU._resource_partners(idx, seeds)
    st = ST.lineage(sg, rg, ST.auto_implicated(sg, rg, task, seeds))
    fld = RU._field_partners(idx, repo, seeds)
    # import-1hop and co-change DO need a size cap (a hub file imported everywhere yields a huge 1-hop), but
    # the cap must rank by the signal's OWN strength, never by content score (that re-dropped dark gold —
    # Pattern A). imports: rank neighbor files by how many seeds they connect to (import centrality);
    # co-change: rank by co-change count. Both caps scale with repo size, so they are not bare constants.
    sig_cap = max(60, total // 150)              # ~60 for normal repos, scales gently for huge monorepos
    imp = RU._import_1hop_ranked(idx, seeds, sig_cap)
    co_all = RU._cochange_counts(idx, repo, seeds)
    co = dict(sorted(co_all.items(), key=lambda kv: -kv[1])[:sig_cap])
    pool = {}

    def add(cid, tag):
        if cid not in rg.nodes:
            return
        n = rg.nodes[cid]
        pool.setdefault(cid, {"qual": n.qual.split("::")[-1], "file": n.file,
                              "lines": f"{n.start}-{n.end}", "tags": []})
        pool[cid]["tags"].append(tag)

    for i, cid in enumerate(sorted(content_score, key=content_score.get, reverse=True)[:content_n]):
        add(cid, f"content#{i+1}")
    for cid in res:
        add(cid, "resource-coupled")
    for cid in st:
        add(cid, "state-producer")
    for cid in fld:
        add(cid, "field-flow")
    for cid in imp:                              # all 1-hop import neighbors, never content-ranked
        add(cid, "import-1hop")
    for cid, c in co.items():                     # all co-changers; the count tag carries the confidence
        add(cid, f"co-changed x{c}")
    # app-config anchors — the fix site for cross-cutting toggles that no proximity/content signal reaches.
    # What counts as an anchor is language-specific (Spring @Configuration/@Enable*, NestJS @Module, ...),
    # so the dominant LanguageProfile decides via is_config_anchor.
    prof = profiles.dominant_profile(rg)
    deco = getattr(rg, "node_decorators", {})
    anchor_ids = set()
    for nid, decs in deco.items():
        if prof.is_config_anchor(decs):
            anchor_ids.add(nid)
            add(nid, "app-config-anchor")
    # package siblings of the STRONG candidates (content seeds + resource/state + anchors). Content-dark
    # gold is very often CO-LOCATED with a strong candidate; include ALL siblings in FOCUSED packages, NO
    # content cap (capping re-drops the content-dark gold this signal exists to catch). "Focused" = small
    # RELATIVE to the repo, not a bare magic number: a focused domain package (config/dto/model) is worth
    # taking whole; a dumping-ground service dir is not. focus_bound scales with repo size (floor 80 so
    # tiny repos still pull focused packages); large packages are still INCLUDED but tagged, never dropped.
    # Only FOCUSED packages get taken whole: co-location is a strong signal inside a small domain package
    # (config/dto/model) but meaningless inside a big dumping-ground service dir (whose gold is reached by
    # content/import/co-change/state instead — verified: scoping this to focused packages keeps recall at
    # 1.00). focus_bound scales with repo size, floor 80, so it is not a bare hand-tuned constant.
    strong = set(seeds) | res | st | anchor_ids
    dir_counts = collections.Counter(os.path.dirname(n.file) for n in nodes_all)
    sib_dirs = {os.path.dirname(rg.nodes[s].file) for s in strong if s in rg.nodes}
    focus_bound = max(80, total // 150)              # focused domain package, min 80; scales for huge repos
    for n in nodes_all:
        d = os.path.dirname(n.file)
        if d in sib_dirs and dir_counts[d] <= focus_bound:
            add(n.id, "package-sibling")
    return pool


def main():
    if len(sys.argv) > 1:
        paths = sys.argv[1:]
    else:
        paths = [p for p in glob.glob("eval/bugs/*.json") if not os.path.basename(p).startswith("_")]
    bugs = D.load_bugs(paths)
    pools, meta = {}, {}
    for bug in bugs:
        idx = cli.fresh_index(bug.repo_dir); rg = idx["rg"]
        gold = M.gold_symbols(rg, bug.gold)
        if not gold:
            continue
        nodes = CH.content_nodes(rg)
        cs, _ = RK.rank_nodes(idx, bug.issue_text, bug.repo_dir, nodes, weights=SL.load_weights(bug.repo_dir))
        seeds = set(sorted(cs, key=cs.get, reverse=True)[:8])
        pool = candidate_pool(idx, bug.issue_text, bug.repo_dir, cs, seeds)
        cf8 = set(sorted(cs, key=cs.get, reverse=True)[:8])
        gold_q = sorted({rg.nodes[g].file + "::" + rg.nodes[g].qual.split("::")[-1] for g in gold})
        # candidate list keyed by file::qual for agent + scoring
        cands = [{"id": f"{v['file']}::{v['qual']}", "qual": v["qual"], "file": v["file"],
                  "lines": v["lines"], "tags": v["tags"]} for v in pool.values()]
        pools[bug.id] = {"issue": bug.issue_text[:900], "candidates": cands}
        pool_ids = {c["id"] for c in cands}
        meta[bug.id] = {
            "gold": gold_q,
            "cf8": sorted({rg.nodes[c].file + "::" + rg.nodes[c].qual.split("::")[-1] for c in cf8}),
            "n_pool": len(cands),
            "ceiling": len(set(gold_q) & pool_ids) / len(gold_q) if gold_q else 0.0,
        }
        print(f"  {bug.id:28s} pool={len(cands):4d}  codefirst@8 recall="
              f"{len(set(gold_q) & set(meta[bug.id]['cf8'])) / len(gold_q):.2f}  ceiling={meta[bug.id]['ceiling']:.2f}")
    json.dump(pools, open("/tmp/cand_pools.json", "w"))
    json.dump(meta, open("/tmp/cand_gold.json", "w"))
    nb = len(meta)
    print(f"\ndumped {nb} pools -> /tmp/cand_pools.json")
    print(f"avg codefirst@8 recall = {sum(len(set(m['gold']) & set(m['cf8']))/len(m['gold']) for m in meta.values())/nb:.2f}")
    print(f"avg pool ceiling       = {sum(m['ceiling'] for m in meta.values())/nb:.2f}")


if __name__ == "__main__":
    main()
