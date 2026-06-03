#!/usr/bin/env python3
"""Agent localization eval — Claude subagent as the LLM walker (no API budget needed: the Agent tool IS
the LLM). Two arms, SAME model, different tools:
  Arm A: Claude + VARD graph tools (search/implicated/impact/lineage/read) + the routing guidance VARD ships
  Arm B: Claude + generic shell only (grep/find/cat)
This isolates VARD's contribution with the LLM held constant — fairer than "vs LocAgent" (which we can't run).

Fixes the n=1 confounds:
  - SCORING is FILE-LEVEL primary (credits valid alternative fix sites; exact-symbol-vs-single-gold is too
    harsh — a localizer that finds the right file has done its job) + a lenient class-level symbol recall.
  - BUG SELECTION targets coupling/dark bugs (where symptom->mechanism reasoning is supposed to fail).
  - Arm A is told to USE the tools (deployment-realistic: VARD ships routing rules that say exactly this).

Usage (the orchestrator — me — drives the subagents; this module preps + scores):
  python -m eval.agent_eval prep  <bug.json>                 # checkout base, dump issue + gold
  python -m eval.agent_eval score <bug.json> <armA.json> <armB.json>   # file + symbol recall per arm
"""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from eval import dataset as D, metric as M
from vard import cli


def prep(path):
    bug = D.load_bug(path)                      # checks out base_commit
    idx = cli.fresh_index(bug.repo_dir); rg = idx["rg"]
    gold = M.gold_symbols(rg, bug.gold)
    goldlist = sorted({rg.nodes[g].file + "::" + rg.nodes[g].qual.split("::")[-1] for g in gold})
    open(f"/tmp/{bug.id}_issue.txt", "w").write(bug.issue_text)
    json.dump(goldlist, open(f"/tmp/{bug.id}_gold.json", "w"))
    print(f"id: {bug.id}")
    print(f"repo: {bug.repo_dir}  (HEAD at base {bug.base_commit})")
    print(f"gold: {len(goldlist)} symbols, {len({g.split('::')[0] for g in goldlist})} files -> /tmp/{bug.id}_gold.json")
    print(f"issue -> /tmp/{bug.id}_issue.txt")
    print("\n--- ISSUE ---\n" + bug.issue_text[:1500])


def _norm(loc):
    f = loc.get("file", "").lstrip("./")
    s = (loc.get("symbol") or "").split("::")[-1]
    return f, s


def score(bugpath, *armfiles):
    bid = json.load(open(bugpath)).get("id") or os.path.splitext(os.path.basename(bugpath))[0]
    gold = set(json.load(open(f"/tmp/{bid}_gold.json")))
    goldf = {g.split("::")[0] for g in gold}
    gold_cls = {(g.split("::")[0], g.split("::")[1].split(".")[0]) for g in gold}   # (file, class)
    print(f"\n  {bid}: gold = {len(gold)} symbols / {len(goldf)} files")
    print(f"  {'arm':22s} {'file-recall':>11s} {'sym-recall':>11s} {'returned':>9s}")
    for af in armfiles:
        arm = json.load(open(af)) if os.path.isfile(af) else af  # path or inline list
        locs = [_norm(x) for x in arm]
        files = {f for f, _ in locs}
        cls = {(f, s.split(".")[0]) for f, s in locs}
        fr = len(files & goldf) / len(goldf) if goldf else 0.0
        sr = len(cls & gold_cls) / len(gold_cls) if gold_cls else 0.0     # class-level symbol recall
        label = os.path.basename(af).replace(".json", "") if isinstance(af, str) else "arm"
        print(f"  {label:22s} {fr:11.2f} {sr:11.2f} {len(locs):9d}")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__); sys.exit(0)
    if sys.argv[1] == "prep":
        prep(sys.argv[2])
    elif sys.argv[1] == "score":
        score(sys.argv[2], *sys.argv[3:])
