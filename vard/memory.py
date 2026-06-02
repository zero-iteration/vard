#!/usr/bin/env python3
"""Codebase memory — the relational layer: join code + state + coupling + the DECISIONS / TICKETS /
INCIDENTS behind a region (mined from history) + what co-changes with it. Answers "what's the WHOLE
picture before I touch X" — the context an agent cannot reconstruct from the code alone (the *why*,
the hidden couplings, the history), which flat memory (CLAUDE.md / grep / vector RAG) does not give.

This is the read/mined layer; a write path (append curated decisions / Jira / postmortems) is the
natural extension — same schema, richer sources.
"""
import os, re, json, subprocess, collections
from . import state as ST

_TICKET = re.compile(r'(?:#(\d+)|\b([A-Z][A-Z0-9]+-\d+)\b)')
_FIX = re.compile(r'\b(fix|bug|hotfix|revert|regression|incident|broke|broken)\b|修复|错误|问题', re.I)
_CODE_EXT = (".java", ".py", ".ts", ".tsx", ".js", ".jsx", ".go")
_TEST = re.compile(r'(/test/|/tests/|/it/|Test\.java$|Tests\.java$|IT\.java$|ITCase\.java$|'
                   r'_test\.|\.test\.|\.spec\.|__tests__)', re.I)


def _load_tickets(repo):
    """Optional ticket-id -> summary map (the intent/Jira corpus). Drop it at <repo>/.vard/tickets.json
    to enrich the 'why' with the business symptom behind each [TF-XXXX], not just the terse commit."""
    p = os.path.join(repo, ".vard", "tickets.json")
    try:
        return json.load(open(p)) if os.path.isfile(p) else {}
    except Exception:
        return {}


def mine_changes(repo, limit=400):
    """The change table (decision/ticket/incident), mined from git history. Safe: returns empty on
    a non-git repo or any git error."""
    try:
        out = subprocess.run(
            ["git", "log", f"-n{limit}", "--no-merges", "--date=short",
             "--pretty=format:@@@%h|%ad|%s", "--name-only"],
            cwd=repo, capture_output=True, text=True, timeout=30).stdout
    except Exception:
        out = ""
    changes, cur = [], None
    for line in out.splitlines():
        if line.startswith("@@@"):
            parts = line[3:].split("|", 2)
            if len(parts) < 3:
                cur = None; continue
            h, d, s = parts
            tickets = sorted({a or b for a, b in _TICKET.findall(s)})
            cur = {"sha": h, "date": d, "subject": s, "files": [],
                   "tickets": tickets, "is_fix": bool(_FIX.search(s))}
            changes.append(cur)
        elif line.strip() and cur is not None and line.strip().endswith(_CODE_EXT):
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
    return {"by_file": by_file, "cochange": cochange}


def whole_picture(idx, repo, target, k=6):
    """Join everything VARD knows about a region into one answer."""
    rg = idx["rg"]
    sg = idx.get("state") or ST.build_state_graph(rg, repo)
    mem = mine_changes(repo)
    tickets_map = _load_tickets(repo)
    cands = [n for n in rg.nodes.values() if target in n.qual or target in n.file]
    if not cands:
        return f"no symbol/file matches '{target}'. Try a class name or a file path fragment."
    # prefer the MAIN class over a test: exact name match, non-test path, class node
    cands.sort(key=lambda n: ((n.name or "") != target, bool(_TEST.search(n.file)), n.type != "class"))
    tfile = cands[0].file
    syms = [n for n in rg.nodes.values()
            if n.file == tfile and n.type in ("function", "method", "class")]

    out = [f"# Whole picture for {tfile}\n", "## Code (symbols here)"]
    for n in syms[:k]:
        out.append(f"- {n.file}:{n.start}-{n.end}  {n.qual}")

    cache = {}
    types_here = sorted({t for n in syms for t in ST._CAP.findall(ST._node_text(repo, n, cache))
                         if t in sg["type_def"] and not ST._INFRA.search(t)})
    if types_here:
        out.append("\n## State it touches (data types)")
        out.append("  " + ", ".join(types_here[:12]))

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

    hist = mem["by_file"].get(tfile, [])
    if hist:
        out.append("\n## Why this code is the way it is (decisions / tickets / incidents)")
        for c in hist[:k]:
            tag = "INCIDENT/fix" if c["is_fix"] else "change"
            tix = f" [{','.join(c['tickets'])}]" if c["tickets"] else ""
            line = f"- {c['date']} {c['sha']} ({tag}){tix}: {c['subject'][:80]}"
            # join the Jira/intent summary if we have the corpus — the business symptom, not just the commit
            summ = next((tickets_map[t] for t in c["tickets"] if t in tickets_map), None)
            if summ:
                line += f"\n    ↳ {summ[:120]}"
            out.append(line)

    co = mem["cochange"].get(tfile)
    if co:
        out.append("\n## Usually changed together with this file")
        for f, n in co.most_common(k):
            out.append(f"- {f}  (x{n})")
    return "\n".join(out)
