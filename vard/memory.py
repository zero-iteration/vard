#!/usr/bin/env python3
"""Codebase memory — the relational layer: join code + state + coupling + the DECISIONS / TICKETS /
INCIDENTS behind a region (mined from history) + what co-changes with it. Answers "what's the WHOLE
picture before I touch X" — the context an agent cannot reconstruct from the code alone (the *why*,
the hidden couplings, the history), which flat memory (CLAUDE.md / grep / vector RAG) does not give.

This is the read/mined layer; a write path (append curated decisions / Jira / postmortems) is the
natural extension — same schema, richer sources.
"""
import os, re, json, subprocess, collections, hashlib, time
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
    _prof = ST.dominant_profile(rg)
    types_here = sorted({t for n in syms for t in ST._CAP.findall(ST._node_text(repo, n, cache))
                         if t in sg["type_def"] and not _prof.infra_re.search(t)})
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


# ============================================================================
# Conversational memory — code-anchored, freshness-verified (the write path).
# A memory is a fact bound to a code anchor (file::symbol). The JSON store is the
# truth; embeddings are only a fallback recall index over the fact text. Invalidation
# rides on the code: at recall we re-hash the cited symbol — changed => flag stale,
# gone => drop. Never inject a silently-stale fact (that is the confident-wrong machine).
# ============================================================================

def _mem_path(repo):
    return os.path.join(os.path.abspath(repo), ".vard", "memory.json")


def load_memories(repo):
    try:
        return json.load(open(_mem_path(repo)))
    except Exception:
        return []


def _save_memories(repo, entries):
    os.makedirs(os.path.join(os.path.abspath(repo), ".vard"), exist_ok=True)
    json.dump(entries, open(_mem_path(repo), "w"), indent=2)


def _anchor_hash(idx, repo, anchor):
    """Freshness hash of an anchor's CURRENT state. Handles two anchor kinds:
       code symbol -> hash of its source span; `config::<key>` -> hash of the key's def sites+values.
    None if the anchor no longer exists (symbol deleted / config key removed)."""
    rg = idx["rg"]
    if anchor.startswith("config::"):                     # config key anchor
        key = anchor.split("::", 1)[1]
        cfg = (idx.get("config") or {}).get(key)
        if not cfg or not cfg.get("defs"):
            return None
        joined = "|".join(f"{d['file']}:{d['line']}={d['value']}" for d in cfg["defs"])
        return hashlib.sha1(joined.encode("utf-8", "ignore")).hexdigest()[:16]
    n = rg.nodes.get(anchor)                              # code symbol anchor
    if n is None:
        return None
    try:
        lines = open(os.path.join(os.path.abspath(repo), n.file), encoding="utf-8", errors="ignore").read().splitlines()
        return hashlib.sha1("\n".join(lines[n.start - 1:n.end]).encode("utf-8", "ignore")).hexdigest()[:16]
    except Exception:
        return None


def _resolve_anchor(idx, citation):
    """Citation -> stable anchor id. Tries a code symbol first (file:line / Class.method / file::qual),
    then a CONFIG KEY (so a fact can be anchored to e.g. 'spring.cache.type'). Returns id or None."""
    from . import query as Q
    ids = Q.resolve_target(idx, citation)
    if ids:
        return ids[0]
    from . import config_index as CFG
    nk = CFG._norm(citation)
    if nk in (idx.get("config") or {}):
        return f"config::{nk}"
    return None


def remember(idx, repo, fact, citations, reason="", source="conversation"):
    """Persist a fact, ANCHORED to code OR config. Anchor-or-drop: a fact with no resolvable citation is
    refused (unanchorable claims can't be invalidated, so they're banned)."""
    rg = idx["rg"]
    cites = [citations] if isinstance(citations, str) else list(citations or [])
    resolved = []
    for c in cites:
        anchor = _resolve_anchor(idx, c)
        if not anchor:
            continue
        if anchor.startswith("config::"):
            key = anchor.split("::", 1)[1]
            defs = (idx.get("config") or {}).get(key, {}).get("defs", [])
            file, name = (defs[0]["file"] if defs else "(config)"), key
        else:
            n = rg.nodes[anchor]; file, name = n.file, n.qual.split("::")[-1]
        resolved.append({"anchor": anchor, "file": file, "name": name,
                         "hash": _anchor_hash(idx, repo, anchor)})
    if not resolved:
        return {"stored": False, "reason": "no citation resolved to code or config — fact refused (anchor-or-drop)"}
    entries = load_memories(repo)
    # write-side adjudication: a new fact on the same anchor SUPERSEDES the old one
    anchors = {r["anchor"] for r in resolved}
    entries = [e for e in entries if not (anchors & {c["anchor"] for c in e.get("citations", [])})]
    entries.append({"fact": fact.strip(), "reason": (reason or "").strip(),
                    "citations": resolved, "source": source, "ts": int(time.time())})
    _save_memories(repo, entries)
    return {"stored": True, "anchors": sorted(anchors), "n_memories": len(entries)}


def _entry_status(idx, repo, entry):
    """active = every cited anchor unchanged; stale = some changed; gone = all deleted."""
    states = []
    for c in entry.get("citations", []):
        cur = _anchor_hash(idx, repo, c["anchor"])
        states.append("gone" if cur is None else ("active" if cur == c.get("hash") else "stale"))
    if states and all(s == "gone" for s in states):
        return "gone"
    return "stale" if "stale" in states or "gone" in states else "active"


def recall(idx, repo, anchors=None, files=None, query="", limit=6):
    """Fresh, relevant memories. Primary match = anchor/file overlap with the current context; embedding
    similarity over the fact text is the fallback. Drops 'gone' memories; flags 'stale' ones for re-check."""
    rg = idx["rg"]
    entries = load_memories(repo)
    if not entries:
        return []
    anchors = set(anchors or []); files = set(files or [])
    scored = []
    for e in entries:
        st = _entry_status(idx, repo, e)
        if st == "gone":
            continue
        ea = {c["anchor"] for c in e["citations"]}; ef = {c["file"] for c in e["citations"]}
        hit = bool((anchors & ea) or (files & ef))
        scored.append((hit, st, e))
    direct = [(st, e) for hit, st, e in scored if hit]
    if direct:
        out = direct
    elif query:                                   # fallback: embedding similarity over fact text
        try:
            from . import embed as E
            import numpy as np
            facts = [e["fact"] for _, _, e in scored]
            fv = E.embed_texts(facts); qv = E.embed_task(query)
            sims = (np.array(fv) @ np.array(qv))
            order = sims.argsort()[::-1][:limit]
            out = [(scored[i][1], scored[i][2]) for i in order if sims[i] > 0.35]
        except Exception:
            out = []
    else:
        out = []
    return [{"fact": e["fact"], "reason": e["reason"], "status": st,
             "anchors": [c["name"] for c in e["citations"]], "ts": e["ts"]} for st, e in out[:limit]]


def recall_text(idx, repo, anchors=None, files=None, query="", limit=6):
    """Render recalled memories for injection — with the freshness flag, never as bare assertion."""
    mems = recall(idx, repo, anchors=anchors, files=files, query=query, limit=limit)
    if not mems:
        return ""
    lines = ["## Remembered about this code (verify flagged items before relying on them)"]
    for m in mems:
        flag = "✓" if m["status"] == "active" else "⚠ cited code CHANGED since — re-check"
        anch = ", ".join(m["anchors"][:3])
        why = f" — {m['reason']}" if m["reason"] else ""
        lines.append(f"  {flag}  {m['fact']}{why}  [{anch}]")
    return "\n".join(lines)
