#!/usr/bin/env python3
"""VARD graph as AGENT TOOLS — to test LocAgent-style localization (an agent navigates the graph) on
VARD's infra, instead of a one-shot static ranking. Each call reloads the cached index (instant) and runs
one tool, so an agent can drive them as shell calls.

  python -m eval.agent_tools <repo> search "<query>" [k]      # ranked spans (BM25+sem+history+PPR)
  python -m eval.agent_tools <repo> impact "<symbol>"          # callers / callees / resource-coupled, with reasons
  python -m eval.agent_tools <repo> def "<name>"               # where a name (class/method) is defined
  python -m eval.agent_tools <repo> imports "<file>"           # 1-hop import neighbours of a file
  python -m eval.agent_tools <repo> lineage "<Type,Type>"      # state lineage: defs + producers/consumers
  python -m eval.agent_tools <repo> read "<file>" <start> <end># show a source span
  python -m eval.agent_tools <repo> whole "<file_or_symbol>"   # whole-picture (code + state + coupling + history)
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vard import cli, rank as RK, selflabel as SL, state as ST, query as Q, propagate as P, memory as MEM
from eval import channels as CH


def search(idx, repo, query, k=12):
    nodes = CH.content_nodes(idx["rg"])
    cs, _ = RK.rank_nodes(idx, query, repo, nodes, weights=SL.load_weights(repo))
    rg = idx["rg"]
    out = []
    for i, nid in enumerate(sorted(cs, key=cs.get, reverse=True)[:k]):
        n = rg.nodes[nid]
        out.append(f"  {i+1:2d}. {n.file}:{n.start}-{n.end}  {n.qual.split('::')[-1]}")
    return "ranked spans:\n" + "\n".join(out)


def impact(idx, symbol):
    r = Q.impact(idx, symbol)
    if r.get("error"):
        return f"impact: {r['error']}"
    lines = [f"target: {r['target']}", f"writes: {r.get('writes')}  reads: {r.get('reads')}"]
    for it in r["items"][:25]:
        lines.append(f"  [{it['relation']:9s}] {it['loc']}  {it['qual'].split('::')[-1]}  — {it['reason'][:80]}")
    return "\n".join(lines)


def definition(idx, name):
    rg = idx["rg"]
    ids = Q.resolve_target(idx, name)
    if not ids:
        return f"def: '{name}' not found"
    return "definitions:\n" + "\n".join(
        f"  {rg.nodes[i].file}:{rg.nodes[i].start}-{rg.nodes[i].end}  {rg.nodes[i].qual.split('::')[-1]} ({rg.nodes[i].type})"
        for i in ids)


def imports(idx, file):
    adj = P.undirected_adj(idx.get("import_edges") or [])
    f = file.lstrip("./")
    nb = sorted(adj.get(f, set()))
    if not nb:
        nb = sorted({x for k, v in adj.items() if f in k for x in v})  # fuzzy
    return f"1-hop import neighbours of {file} ({len(nb)}):\n" + "\n".join("  " + x for x in nb[:40])


def lineage(idx, repo, types):
    rg = idx["rg"]
    sg = idx.get("state") or ST.build_state_graph(rg, repo)
    ids = ST.lineage(sg, rg, [t.strip() for t in types.split(",")])
    return f"state lineage for {types} ({len(ids)} spans):\n" + "\n".join(ST.render(rg, ids)[:40])


def implicated(idx, repo, issue):
    """What STATE (data types) an issue implicates — the gated cache/queue/holder path. This is the tool
    that hands the agent the dark coupling payloads (e.g. the cached CO types) it can't reach by search."""
    rg = idx["rg"]
    sg = idx.get("state") or ST.build_state_graph(rg, repo)
    nodes = CH.content_nodes(rg)
    cs, _ = RK.rank_nodes(idx, issue, repo, nodes, weights=SL.load_weights(repo))
    seeds = set(sorted(cs, key=cs.get, reverse=True)[:8])
    types = sorted(ST.auto_implicated(sg, rg, issue, seeds))
    # show each implicated type + where it's defined (so the agent can open it)
    out = [f"implicated state types ({len(types)}): {types}", "definitions:"]
    for t in types:
        for did in sg["type_def"].get(t, [])[:1]:
            n = rg.nodes[did]
            out.append(f"  {t:18s} {n.file}:{n.start}-{n.end}")
    return "\n".join(out)


def read(idx, repo, file, start, end):
    try:
        lines = open(os.path.join(repo, file.lstrip("./")), errors="ignore").read().splitlines()
    except Exception as e:
        return f"read error: {e}"
    s, e = max(1, int(start)), min(len(lines), int(end))
    return "\n".join(f"{i:5d}| {lines[i-1]}" for i in range(s, e + 1))


def main():
    if len(sys.argv) < 3:
        print(__doc__); return
    repo, tool = sys.argv[1], sys.argv[2]
    repo = os.path.abspath(os.path.expanduser(repo))
    idx = cli.fresh_index(repo)
    a = sys.argv[3:]
    if tool == "search":
        print(search(idx, repo, a[0], int(a[1]) if len(a) > 1 else 12))
    elif tool == "impact":
        print(impact(idx, a[0]))
    elif tool == "def":
        print(definition(idx, a[0]))
    elif tool == "imports":
        print(imports(idx, a[0]))
    elif tool == "lineage":
        print(lineage(idx, repo, a[0]))
    elif tool == "implicated":
        print(implicated(idx, repo, a[0]))
    elif tool == "read":
        print(read(idx, repo, a[0], a[1], a[2]))
    elif tool == "whole":
        print(MEM.whole_picture(idx, a[0], repo))
    else:
        print(f"unknown tool {tool}"); print(__doc__)


if __name__ == "__main__":
    main()
