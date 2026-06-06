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


def _resolve_region(idx, repo, target, mem):
    """Resolve an explain target → (tfile, focal_syms, ticket). Accepts a symbol/file (like whole_picture)
    or a ticket id (#123 / ABC-12) — for a ticket we pick the most-touched non-test code file it changed."""
    rg = idx["rg"]
    tm = _TICKET.search(target.strip())
    ticket = (tm.group(1) and f"#{tm.group(1)}") or (tm.group(2) if tm else None) if tm else None
    # a ticket target only counts if the whole target IS the ticket (avoid matching a number inside a name)
    if ticket and target.strip().lstrip("#") in (ticket.lstrip("#"),):
        scored = [(f, sum(1 for c in cs if ticket.lstrip("#") in {t.lstrip("#") for t in c["tickets"]}))
                  for f, cs in mem["by_file"].items()]
        scored = [(f, n) for f, n in scored if n and not _TEST.search(f)]
        scored.sort(key=lambda x: -x[1])
        if scored:
            tfile = scored[0][0]
            syms = [n for n in rg.nodes.values() if n.file == tfile and n.type in ("function", "method", "class")]
            return tfile, syms, ticket
    ticket = None
    cands = [n for n in rg.nodes.values() if target in n.qual or target in n.file]
    if not cands:
        return None, [], None
    cands.sort(key=lambda n: ((n.name or "") != target, bool(_TEST.search(n.file)), n.type != "class"))
    tfile = cands[0].file
    syms = [n for n in rg.nodes.values() if n.file == tfile and n.type in ("function", "method", "class")]
    return tfile, syms, None


def explain(idx, repo, target, k=8):
    """The actual-vs-expected JOIN — VARD's confident answer. It never claims to find the bug; it makes the
    DIVERGENCE undeniable by joining every leg and tagging each line with its provenance:

      ACTUAL      — what we OBSERVED run (runtime overlay)            [confirmed-runtime]
      MECHANISM   — the code + the commit/ticket that introduced it   [code] / [commit]
      EXPECTED    — what you told us you expected (typed memory) + ticket text   [your expectation] / [ticket]
      CONFIG      — the settings that steer it (file-values)          [config]
      DIVERGENCE  — explicit, groundable conflicts                    [divergence]
      UNCERTAINTY — what we could NOT confirm (never guessed)         [unverified]
    """
    rg = idx["rg"]
    mem = mine_changes(repo)
    tickets_map = _load_tickets(repo)
    tfile, syms, ticket = _resolve_region(idx, repo, target, mem)
    if not tfile:
        return f"no symbol/file/ticket matches '{target}'. Try a class name, a file path fragment, or a ticket id."
    focus_ids = {n.id for n in syms}
    rt_conf = idx.get("rt_confirmed") or set()
    rt_traced = idx.get("rt_traced") or set()        # ever-observed (ignores freshness)
    rt_edges = idx.get("rt_edges") or []
    rt_values = idx.get("rt_values") or {}           # anchor -> [{v, n, envs}] (instrumentation)
    rt_runs = idx.get("rt_runs") or {}               # {env: {profile, mode}}
    rt_method_envs = idx.get("rt_method_envs") or {} # anchor -> {env: hits}
    has_runtime = bool(rt_conf or rt_traced)

    def _is_testish(env):                            # an env/profile that looks like a test path, not prod
        e = (env or "").lower()
        prof = (rt_runs.get(env, {}).get("profile") or "").lower()
        return "test" in e or "test" in prof or e == "default"

    hdr = f"# Explain: {target}"
    if ticket:
        hdr += f"  (ticket {ticket} → {tfile})"
    out = [hdr, f"_actual-vs-expected for {tfile} — every line tagged with how we know it_\n"]

    # ---------- ACTUAL (grounded): what we observed run, under which env, with the real values ----------
    if rt_runs:
        runlbl = ", ".join(f"{e}(profile={v.get('profile') or '?'})" for e, v in rt_runs.items())
        runfp = f" — runs merged in: {runlbl}"
    else:
        runfp = " — no run captured" if not has_runtime else " — env not captured"
    out.append(f"## ACTUAL — what actually runs (observed{runfp})")
    if not has_runtime:
        out.append("  [unverified] no runtime trace for this repo — run `vard test -- <cmd>` (or `vard attach <pid>`) to ground this leg.")
    else:
        confirmed_here = [n for n in syms if n.id in rt_conf]
        stale_here = [n for n in syms if n.id in rt_traced and n.id not in rt_conf]
        for n in confirmed_here[:k]:
            envs = rt_method_envs.get(n.id, {})
            envlbl = f"  [under {', '.join(sorted(envs))}]" if envs else ""
            out.append(f"  [confirmed-runtime] {n.qual}  ({n.file}:{n.start}) — observed executing{envlbl}")
            for s in (rt_values.get(n.id) or [])[:4]:    # the agent-uncatchable fact: real args ⇒ real return
                out.append(f"      [observed-value] {n.name}{s['v']}   ({s['n']}x)")
        for n in stale_here[:k]:                      # observed in an earlier run, but the code changed since
            out.append(f"  [stale-trace] {n.qual}  ({n.file}:{n.start}) — observed before, but its code "
                       f"CHANGED since the trace; re-run `vard test`")
        if not confirmed_here and not stale_here:
            out.append("  [unverified] none of this file's methods were seen in the captured trace "
                       "(not exercised by the tests, or a different path runs).")
        edges = [(a, b, c) for a, b, c in rt_edges if a in focus_ids or b in focus_ids]
        for a, b, c in edges[:k]:
            out.append(f"  [confirmed-runtime] {rg.nodes[a].qual} → {rg.nodes[b].qual}  ({c}x) — real call observed")

    # ---------- MECHANISM: the code + why it's coded this way ----------
    out.append("\n## MECHANISM — the code, and why it's this way")
    for n in syms[:k]:
        out.append(f"  [code] {n.qual}  ({n.file}:{n.start}-{n.end})")
    hist = mem["by_file"].get(tfile, [])
    for c in hist[:k]:
        tag = "INCIDENT/fix" if c["is_fix"] else "change"
        tix = f" {','.join('#'+t.lstrip('#') for t in c['tickets'])}" if c["tickets"] else ""
        out.append(f"  [commit {c['sha']}] {c['date']} ({tag}){tix}: {c['subject'][:80]}")
        summ = next((tickets_map[t] for t in c["tickets"] if t in tickets_map), None)
        if summ:
            out.append(f"      ↳ [ticket] {summ[:120]}")

    # ---------- EXPECTED: what you told us you expected (the oracle) ----------
    out.append("\n## EXPECTED — what you expected (your corrections / intent)")
    exp = recall(idx, repo, anchors=focus_ids, files={tfile}, query=target, limit=k, kinds={"expectation"})
    if exp:
        for m in exp:
            flag = "" if m["status"] == "active" else "  ⚠(cited code changed since you said this)"
            out.append(f"  [your expectation] {m['fact']}  [{', '.join(m['anchors'][:3])}]{flag}")
    else:
        out.append("  [unverified] no recorded expectation for this region — capture one with "
                   "`vard expect \"<what you expected>\" <symbol>`.")
    if ticket and tickets_map.get(ticket) or (ticket and tickets_map.get(ticket.lstrip("#"))):
        out.append(f"  [ticket] {tickets_map.get(ticket) or tickets_map.get(ticket.lstrip('#'))}")

    # ---------- CONFIG: settings that steer it ----------
    cfg = idx.get("config") or {}
    cfg_here = []                                          # (key, [ (value,file,line) ... ])
    for key, v in cfg.items():
        if set(v["readers"]) & focus_ids and v.get("defs"):
            cfg_here.append((v["defs"][0]["key"], [(d["value"], d["file"], d["line"]) for d in v["defs"]]))
    if cfg_here:
        out.append("\n## CONFIG — settings this code reads (steers behavior, not in the code)")
        for key, defs in cfg_here[:k]:
            vals = "; ".join(f"{val} ({os.path.basename(f)}:{ln})" for val, f, ln in defs[:4])
            out.append(f"  [config] {key} = {vals}")

    # ---------- DIVERGENCE: groundable conflicts (the undeniable part) ----------
    div = []
    # D1 expected-but-not-observed: an expectation anchored to a method that did NOT run in the trace. Only
    # fire for methods NEVER observed (untested / different path). Methods that WERE observed but went stale
    # are a trace-freshness issue (surfaced as [stale-trace] above + D2), not a behavioral divergence.
    if has_runtime:
        for m in exp:
            anchored_methods = [a for a in m.get("anchor_ids", [])
                                if a in rg.nodes and rg.nodes[a].type in ("function", "method")]
            never = [a for a in anchored_methods if a not in rt_conf and a not in rt_traced]
            if never:
                div.append(f"[divergence] you expect behavior at {rg.nodes[never[0]].qual}, but it was NEVER "
                           f"observed running in the trace — either it's untested or a DIFFERENT path executes. "
                           f"(expected: “{m['fact'][:80]}”)")
    # D2 stale expectation: the code an expectation cites has changed since it was stated
    for m in exp:
        if m["status"] != "active":
            div.append(f"[divergence] your expectation “{m['fact'][:70]}” cites code that CHANGED since — "
                       f"it may no longer hold; re-confirm.")
    # D4 value juxtaposition: an expectation anchored to a method we OBSERVED returning concrete values.
    # We don't parse the expectation (no fragile NLP) — we put the expected claim next to the REAL observed
    # values so the contradiction (if any) is undeniable and the agent adjudicates. This is the killer leg:
    # "you expected X; it actually returned these numbers."
    for m in exp:
        if m["status"] != "active":
            continue
        for a in m.get("anchor_ids", []):
            obs = rt_values.get(a)
            if obs:
                vs = "; ".join(s["v"] for s in obs[:3])
                div.append(f"[divergence-check] you expect “{m['fact'][:70]}” at {rg.nodes[a].qual}; it was "
                           f"OBSERVED returning: {vs} — confirm the numbers actually agree with that.")
    # D3 config-profile ambiguity: a key this code reads has multiple defs with DIFFERENT values → which wins?
    for key, defs in cfg_here:
        distinct = {val for val, _, _ in defs}
        if len(distinct) > 1:
            div.append(f"[divergence] behavior depends on `{key}`, defined with conflicting values "
                       f"({', '.join(sorted(distinct)[:4])}) across profiles — which one is live depends on the "
                       f"active profile, which the trace does NOT capture. Verify the running profile.")
    if div:
        out.append("\n## DIVERGENCE — where ACTUAL and EXPECTED don't line up")
        for d in div:
            out.append("  " + d)

    # ---------- UNCERTAINTY: state the gaps, never guess (anti-anchor) ----------
    unc = []
    if not has_runtime:
        unc.append("the ACTUAL leg is ungrounded (no trace) — claims about what runs are unconfirmed.")
    # masquerade guard: if every confirmed method here was only seen under a test-ish env, ACTUAL is the
    # TEST path, not necessarily prod. Don't let a test-profile run pass for prod truth.
    if has_runtime and confirmed_here:
        seen_envs = set().union(*[set(rt_method_envs.get(n.id, {})) for n in confirmed_here]) or set()
        if seen_envs and all(_is_testish(e) for e in seen_envs):
            unc.append(f"everything here was observed only under a test path ({', '.join(sorted(seen_envs))}) — "
                       f"this may NOT match prod. Trace prod/staging with `vard attach <pid> --env prod` to confirm.")
    if exp and not has_runtime:
        unc.append("expectations are recorded but cannot be checked against a run — capture a trace to confirm them.")
    if unc:
        out.append("\n## UNCERTAINTY — what we could not confirm")
        for u in unc:
            out.append(f"  [unverified] {u}")
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


# A fact's KIND decides which side of the actual-vs-expected join it feeds:
#   mechanism   — WHY the code is the way it is (a decision/constraint/gotcha). Default.
#   expectation — what the user EXPECTED / intended / corrected ("cheaper option should win"). The oracle.
#   observation — something noted as seen happening (a manual runtime note; runtime.json is the automatic one).
MEM_KINDS = ("mechanism", "expectation", "observation")


def _entry_kind(entry):
    k = entry.get("kind")
    return k if k in MEM_KINDS else "mechanism"      # legacy/untyped facts read as mechanism


def remember(idx, repo, fact, citations, reason="", source="conversation", kind="mechanism"):
    """Persist a fact, ANCHORED to code OR config. Anchor-or-drop: a fact with no resolvable citation is
    refused (unanchorable claims can't be invalidated, so they're banned). `kind` tags which side of the
    join it feeds (mechanism / expectation / observation) — see MEM_KINDS."""
    kind = kind if kind in MEM_KINDS else "mechanism"
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
    # write-side adjudication: a new fact on the same anchor SUPERSEDES the old one of the SAME KIND
    # (an expectation doesn't clobber the mechanism on the same code, and vice-versa — they coexist).
    anchors = {r["anchor"] for r in resolved}
    entries = [e for e in entries
               if not (anchors & {c["anchor"] for c in e.get("citations", [])} and _entry_kind(e) == kind)]
    entries.append({"fact": fact.strip(), "reason": (reason or "").strip(),
                    "citations": resolved, "source": source, "kind": kind, "ts": int(time.time())})
    _save_memories(repo, entries)
    return {"stored": True, "anchors": sorted(anchors), "kind": kind, "n_memories": len(entries)}


def _entry_status(idx, repo, entry):
    """active = every cited anchor unchanged; stale = some changed; gone = all deleted."""
    states = []
    for c in entry.get("citations", []):
        cur = _anchor_hash(idx, repo, c["anchor"])
        states.append("gone" if cur is None else ("active" if cur == c.get("hash") else "stale"))
    if states and all(s == "gone" for s in states):
        return "gone"
    return "stale" if "stale" in states or "gone" in states else "active"


def recall(idx, repo, anchors=None, files=None, query="", limit=6, kinds=None):
    """Fresh, relevant memories. Primary match = anchor/file overlap with the current context; embedding
    similarity over the fact text is the fallback. Drops 'gone' memories; flags 'stale' ones for re-check.
    `kinds` optionally restricts to a set of MEM_KINDS (e.g. {'expectation'} for the EXPECTED leg)."""
    rg = idx["rg"]
    entries = load_memories(repo)
    if not entries:
        return []
    anchors = set(anchors or []); files = set(files or [])
    scored = []
    for e in entries:
        if kinds and _entry_kind(e) not in kinds:
            continue
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
    return [{"fact": e["fact"], "reason": e["reason"], "status": st, "kind": _entry_kind(e),
             "anchors": [c["name"] for c in e["citations"]],
             "anchor_ids": [c["anchor"] for c in e["citations"]], "ts": e["ts"]} for st, e in out[:limit]]


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
        kind = f"{m['kind']}: " if m.get("kind") and m["kind"] != "mechanism" else ""
        lines.append(f"  {flag}  {kind}{m['fact']}{why}  [{anch}]")
    return "\n".join(lines)
