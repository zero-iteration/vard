#!/usr/bin/env python3
"""Codebase memory DB — prototype of the real thesis: a RELATIONAL store the agent queries to get
the WHOLE PICTURE, not a flat scan.

Schema (tables):
  symbol   : code (file, qual, type)                      [from VARD's symbol graph]
  state    : data/types the program is about               [from VARD's state graph]
  change   : a commit = a DECISION/why (+ ticket refs + is_fix => incident)  [from git history]
Joins:
  symbol  --transforms-->  state            (state graph: refs/producers)
  state   --coupled_with-> state            (coupling layer: writer<->reader)
  file    --touched_by-->  change           (commit file lists)
  change  --references-->  ticket           (#123 / ABC-123 in the message)
  file    --co_changes-->  file             (files committed together)

Query: whole_picture(target) -> the code + the state it touches + coupled state + the decisions/
tickets/incidents behind it + what historically co-changes with it. That join is what flat memory
(CLAUDE.md, grep, vector RAG) cannot give.
"""
import sys, os, re, subprocess, collections
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vard import cli, state as ST

_TICKET = re.compile(r'(?:#(\d+)|\b([A-Z][A-Z0-9]+-\d+)\b)')
_FIX = re.compile(r'\b(fix|bug|hotfix|revert|regression|incident|broke|broken)\b|修复|错误|问题', re.I)


def mine_changes(repo, limit=400):
    """The change/decision/ticket/incident table, mined from git history."""
    out = subprocess.run(
        ["git", "log", f"-n{limit}", "--no-merges", "--date=short",
         "--pretty=format:@@@%h|%ad|%s", "--name-only"],
        cwd=repo, capture_output=True, text=True).stdout
    changes, cur = [], None
    for line in out.splitlines():
        if line.startswith("@@@"):
            h, d, s = line[3:].split("|", 2)
            tickets = sorted({a or b for a, b in _TICKET.findall(s)})
            cur = {"sha": h, "date": d, "subject": s, "files": [],
                   "tickets": tickets, "is_fix": bool(_FIX.search(s))}
            changes.append(cur)
        elif line.strip() and cur is not None and line.endswith((".java", ".py", ".ts", ".js", ".go")):
            cur["files"].append(line.strip())
    by_file = collections.defaultdict(list)
    cochange = collections.defaultdict(collections.Counter)
    for c in changes:
        for f in c["files"]:
            by_file[f].append(c)
        for a in c["files"]:
            for b in c["files"]:
                if a != b:
                    cochange[a][b] += 1
    return {"changes": changes, "by_file": by_file, "cochange": cochange}


def whole_picture(repo, target, k=6):
    repo = os.path.abspath(repo)
    idx = cli.fresh_index(repo)
    rg = idx["rg"]
    sg = idx.get("state") or ST.build_state_graph(rg, repo)
    mem = mine_changes(repo)

    # resolve target -> a file + the symbols in it
    nodes = [n for n in rg.nodes.values() if target in n.qual or target in n.file]
    if not nodes:
        return f"no symbol/file matches '{target}'"
    tfile = nodes[0].file
    syms = [n for n in rg.nodes.values() if n.file == tfile and n.type in ("function", "method", "class")]

    out = [f"# WHOLE PICTURE for {tfile}\n"]
    # CODE
    out.append("## Code (symbols here)")
    for n in syms[:k]:
        out.append(f"- {n.file}:{n.start}-{n.end}  {n.qual}")
    # STATE it transforms (types defined/used here)
    cache = {}
    types_here = sorted({t for n in syms for t in ST._CAP.findall(ST._node_text(repo, n, cache))
                         if t in sg["type_def"] and not ST._INFRA.search(t)})
    if types_here:
        out.append("\n## State it touches (data types)")
        out.append("  " + ", ".join(types_here[:12]))
    # COUPLED state (writer<->reader through shared data)
    res = idx.get("res") or {}
    coupled = set()
    for rid in res.get("nodes", []):
        owners = set(res.get("writers", {}).get(rid, [])) | set(res.get("readers", {}).get(rid, []))
        if any(rg.nodes.get(o) and rg.nodes[o].file == tfile for o in owners):
            for o in owners:
                if rg.nodes.get(o) and rg.nodes[o].file != tfile:
                    coupled.add((rg.nodes[o].file, rid))
    if coupled:
        out.append("\n## Coupled through shared data (you'd break these)")
        for f, rid in list(coupled)[:k]:
            out.append(f"- {f}   ⮂ shares {rid}")
    # DECISIONS / TICKETS / INCIDENTS (the why — from history, joined on this file)
    hist = mem["by_file"].get(tfile, [])
    if hist:
        out.append("\n## Why this code is the way it is (decisions / tickets / incidents)")
        for c in hist[:k]:
            tag = "INCIDENT/fix" if c["is_fix"] else "change"
            tix = f" [{','.join(c['tickets'])}]" if c["tickets"] else ""
            out.append(f"- {c['date']} {c['sha']} ({tag}){tix}: {c['subject'][:80]}")
    # CO-CHANGES (what you'll likely also need to touch)
    co = mem["cochange"].get(tfile)
    if co:
        out.append("\n## Historically changed together with this file")
        for f, n in co.most_common(k):
            out.append(f"- {f}  (x{n})")
    return "\n".join(out)


if __name__ == "__main__":
    repo = sys.argv[1]
    target = sys.argv[2] if len(sys.argv) > 2 else ""
    print(whole_picture(repo, target))
