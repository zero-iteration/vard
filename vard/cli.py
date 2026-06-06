#!/usr/bin/env python3
"""
vard — repository attention for AI coding agents (stack-agnostic, key-optional).

  vard init <repo>                 # discover + index (run once)
  vard couplings <repo>            # list implicit data-coupling (writer <-> reader)
  vard context "<task>" <repo>     # retrieve relevant code + coupled partners

Shared core functions (build_index / couplings_text / context_text) are reused by
the MCP server so an agent can call the same logic.
"""
import argparse, os, pickle, re, sys
from . import common as C, graph as G, resources as R, freshness as Fr

VDIR = ".vard"


def _index_path(repo): return os.path.join(os.path.abspath(repo), VDIR, "index.pkl")


# Routing rules an AI agent should follow to use VARD proactively. `vard rules` prints this;
# `vard rules --write` drops it into the repo's CLAUDE.md / AGENTS.md (idempotent) so the agent
# picks it up automatically and stops being told "use vard" every time.
AGENT_RULES = """<!-- vard:routing (managed by `vard rules` — safe to leave; re-run to update) -->
## Code retrieval with VARD

This repo is indexed by **VARD** (a local symbol-graph + data-coupling code retriever; MCP server `vard`).
Use it proactively, without being asked:

- **Locating code** ("where is X", "what handles Y", understanding a feature, or gathering context
  before a multi-file change): call `vard_context("<task in plain words>")` **first**, before grepping
  or reading files, and use the returned `file:line` spans as your starting set.
- **Before editing code that touches shared state**: call `vard_impact("<QualifiedName or file.py:line>")`
  for the blast radius (readers/writers coupled through caches, DBs, or queues).
- **Tracing a resource**: `vard_resource("<table / cache-key / queue>")` for who writes vs reads it.
- **Data is wrong / stale / incomplete and the code that sets it isn't obvious** (state-first localization):
  call `vard_state_candidates("<task>")` to see the program's state types, identify which hold the WRONG
  state (including state the symptom doesn't name), then `vard_state_lineage("TypeA, TypeB")` to get the code
  that defines and produces/consumes it — including producers in other modules with no textual link to the bug.
- **Before editing a file, or whenever you need the whole picture (not just the code)**: call
  `vard_whole_picture("<Class or file>")` — it joins the code, the state it touches, the code coupled
  through shared data, the decisions/tickets/incidents behind it (why it's this way, from history), and
  what co-changes with it. This is context you cannot reconstruct by reading the code.
- **When the user tells you something durable that ISN'T in the code** — a decision, constraint, gotcha,
  or correction ("this cache is the source of truth, not the DB"; "never call X directly, it skips
  validation"; "we did it this way because of the 2023 incident") — call `vard_remember("<fact>",
  "<symbol or file:line it's about>")` so future sessions aren't re-told. It auto-expires when that code
  changes, so it can't go stale.
- **Before answering how/why code behaves**, call `vard_recall("<task>")` to surface what the user already
  told you about it (each fact is freshness-checked: ✓ valid, ⚠ cited code changed — re-check).
- Skip VARD for trivial edits to a file you already have open.

If the MCP tools aren't loaded, the CLI is equivalent: `vard context "..."`, `vard impact <name>`,
`vard resource <name>`, `vard couplings`, `vard remember "<fact>" "<anchor>"`, `vard recall "<task>"`.
The index self-refreshes; run `vard init` once if `.vard/` is absent.
<!-- /vard:routing -->
"""


def _write_rules(repo, fname=None):
    """Add/refresh the VARD routing block in the repo's agent config (CLAUDE.md or AGENTS.md)."""
    import re
    repo = os.path.abspath(repo)
    if fname:
        target = os.path.join(repo, fname)
    else:
        target = next((os.path.join(repo, c) for c in ("CLAUDE.md", "AGENTS.md")
                       if os.path.isfile(os.path.join(repo, c))), os.path.join(repo, "CLAUDE.md"))
    existing = open(target, encoding="utf-8", errors="ignore").read() if os.path.isfile(target) else ""
    if "<!-- vard:routing" in existing:
        new = re.sub(r"<!-- vard:routing.*?<!-- /vard:routing -->\n?", AGENT_RULES, existing, flags=re.S)
        open(target, "w").write(new)
        return f"↻ refreshed VARD routing in {target}"
    glue = "" if not existing else ("\n" if existing.endswith("\n") else "\n\n")
    open(target, "a").write(glue + AGENT_RULES)
    return f"added VARD routing to {os.path.basename(target)}"


def _wire_mcp(repo):
    """Best-effort: register the VARD MCP server with Claude Code so the agent can call it natively.
    Skips silently if the `claude` CLI isn't present (routing rules + CLI still work without MCP)."""
    import shutil, subprocess
    claude = shutil.which("claude")
    if not claude:
        return None
    try:                                              # already registered?
        g = subprocess.run([claude, "mcp", "get", "vard"], capture_output=True, text=True, timeout=15, cwd=repo)
        if g.returncode == 0 and "vard" in (g.stdout or ""):
            return "MCP server 'vard' already registered"
    except Exception:
        pass
    mcp_bin = shutil.which("vard-mcp") or "vard-mcp"
    try:
        r = subprocess.run([claude, "mcp", "add", "vard", "--", mcp_bin], capture_output=True, text=True, timeout=20, cwd=repo)
        if r.returncode == 0:
            return "registered MCP server 'vard' — restart your agent to load its tools"
    except Exception:
        pass
    return f"couldn't auto-register MCP; run:  claude mcp add vard -- {mcp_bin}"


def _project_root(repo):
    """Resolve to the multi-module project root (Maven reactor / Gradle root) so we index the WHOLE
    project, not just the dir pointed at. Set VARD_NO_REACTOR=1 to index only the given dir."""
    repo = os.path.abspath(repo)
    if os.environ.get("VARD_NO_REACTOR"):
        return repo
    try:
        from . import deps as DEP
        return DEP.find_project_root(repo)
    except Exception:
        return repo


def build_index(repo, fresh=False, llm=None, extra_roots=None):
    """Build & cache the attention graph + resource layer. Indexes the whole multi-module project
    (reactor root) plus any extra source roots (dependency modules outside the tree).
    llm: optional agent LLM for discovery."""
    repo = _project_root(repo)
    extra_roots = [os.path.abspath(r) for r in (extra_roots or []) if os.path.isdir(r)]
    if not os.environ.get("VARD_NO_DEPS"):              # auto-discover co-located source deps by default
        try:
            from . import deps as DEP
            disc = [os.path.abspath(d) for d in DEP.discover_source_deps(repo) if os.path.isdir(d)]
            extra_roots = sorted(set(extra_roots) | set(disc))
        except Exception:
            pass
    os.makedirs(os.path.join(repo, VDIR), exist_ok=True)
    try:
        from . import discover as D
        rs = D.discover(repo, use_cache=not fresh, llm=llm)
        src = "agent" if llm else ("openai" if os.environ.get("VARD_DISCOVER", "").lower() == "openai" else "default")
    except Exception as e:
        rs, src = R.DEFAULT_RULESET, f"default ({str(e)[:40]})"
    try:
        from . import deps as DEP
        mods = DEP.find_modules(repo)
        if mods:
            print(f"→ vard: multi-module project — indexing {len(mods)+1} modules from {os.path.basename(repo)}/",
                  file=sys.stderr, flush=True)
        if extra_roots:
            print(f"→ vard: + {len(extra_roots)} extra source root(s): "
                  + ", ".join(os.path.basename(r) for r in extra_roots), file=sys.stderr, flush=True)
    except Exception:
        pass
    print("→ vard: parsing source files (tree-sitter)...", file=sys.stderr, flush=True)
    rg = G.build_graph(repo, extra_roots=extra_roots)   # whole project + extra source roots
    nfiles = len({n.file for n in rg.nodes.values()}); skipped = getattr(rg, "skipped", 0)
    msg = f"→ vard: {len(rg.nodes)} symbols across {nfiles} files"
    if skipped:
        msg += f" ({skipped} files skipped: parse errors)"
    print(msg, file=sys.stderr, flush=True)
    if len(rg.nodes) == 0:
        print("⚠ vard: no code symbols found — is this a supported-language repo (py/java/js/ts/go)?", file=sys.stderr, flush=True)
    # Enrichment layers are ADDITIVE and best-effort: any failure degrades to "without it",
    # it must NEVER kill the index (the symbol graph is the only essential part).
    res = {"nodes": [], "edges": [], "writers": {}, "readers": {}}; rx = {"resource_nodes": 0, "n_edges": 0}
    try:
        ext = R.extract(rg, rs)
        res = {"nodes": ext.res_nodes, "edges": ext.edges,
               "writers": {k: list(v) for k, v in ext.writers.items()},
               "readers": {k: list(v) for k, v in ext.readers.items()}}
        rx = ext.stats()
    except Exception as e:
        print(f"⚠ vard: coupling extraction failed ({str(e)[:50]}) — indexing without the coupling layer", file=sys.stderr, flush=True)
    from . import history as H
    print("→ vard: mining commit history + import graph...", file=sys.stderr, flush=True)
    hist = H.mine(repo)                  # internally safe: returns [] if git/history unavailable
    from . import propagate as P
    code_files = sorted({n.file for n in rg.nodes.values()})
    try:
        import_edges = P.build_import_edges(repo, code_files)   # static file→file graph for query-time PPR
    except Exception as e:
        print(f"⚠ vard: import-graph build failed ({str(e)[:50]}) — indexing without graph-PPR", file=sys.stderr, flush=True)
        import_edges = []
    try:
        from . import state as ST
        print("→ vard: building state graph (types + producers/consumers)...", file=sys.stderr, flush=True)
        state_graph = ST.build_state_graph(rg, repo)
    except Exception as e:
        print(f"⚠ vard: state-graph build failed ({str(e)[:50]}) — indexing without state lineage", file=sys.stderr, flush=True)
        state_graph = None
    try:
        from . import config_index as CFG
        print("→ vard: indexing config/properties (runtime settings + their code readers)...", file=sys.stderr, flush=True)
        config = CFG.build_config_index(rg, repo)
    except Exception as e:
        print(f"⚠ vard: config indexing failed ({str(e)[:50]}) — indexing without the config layer", file=sys.stderr, flush=True)
        config = {}
    try:
        with open(_index_path(repo), "wb") as f:
            pickle.dump({"rg": rg, "ruleset": rs, "fingerprint": Fr.fingerprint(repo), "history": hist,
                         "import_edges": import_edges, "res": res, "state": state_graph,
                         "config": config, "extra_roots": extra_roots}, f)
    except Exception as e:
        raise RuntimeError(f"could not write index to {_index_path(repo)}: {e}") from e
    gs = rg.stats()
    return {"repo": os.path.basename(repo), "ruleset_source": src, "code_nodes": gs["n_nodes"],
            "code_edges": gs["n_edges"], "resources": rx["resource_nodes"],
            "resource_edges": rx["n_edges"], "index": _index_path(repo)}


def load_index(repo):
    p = _index_path(repo)
    if not os.path.isfile(p):
        return None
    try:
        return pickle.load(open(p, "rb"))
    except Exception:
        return None        # stale/incompatible cache → caller rebuilds


def fresh_index(repo):
    """Persistent + self-updating: instant when the repo is unchanged; re-index only
    when files changed (graph rebuild is cheap; embeddings update only for changed code)."""
    repo = _project_root(repo)
    idx = load_index(repo)
    cur = Fr.fingerprint(repo)
    if idx is None:
        print("→ vard: building index (first run)...", file=sys.stderr)
        build_index(repo); idx = load_index(repo)
    elif not Fr.is_fresh(idx.get("fingerprint", {}), cur):
        changed, deleted = Fr.diff(idx.get("fingerprint", {}), cur)
        print(f"→ vard: repo changed ({len(changed)} modified, {len(deleted)} removed) — re-indexing...", file=sys.stderr)
        build_index(repo, extra_roots=idx.get("extra_roots")); idx = load_index(repo)
    # runtime overlay (ground-truth executed code/edges) lives in .vard/runtime.json — it accretes between
    # re-indexes and is freshness-checked per node, so it's attached on load, not baked into the pickle.
    if idx is not None:
        from . import runtime as RT
        RT.attach(idx, repo)
    return idx


def _is_test(path):
    p = path.lower()
    return ("/test" in p or "test_" in p or p.endswith(("test.java", ".test.ts", ".test.js", ".spec.ts", ".spec.js"))
            or "/spec/" in p or "__tests__" in p)


def _pairs(idx, skip_tests=True):
    rg, res = idx["rg"], idx["res"]
    out = []
    for rid in res["nodes"]:
        for w in res["writers"].get(rid, []):
            for r in res["readers"].get(rid, []):
                if w == r or w.endswith("<module>") or r.endswith("<module>"):
                    continue
                if skip_tests and (_is_test(rg.nodes[w].file) or _is_test(rg.nodes[r].file)):
                    continue
                out.append((rg.nodes[w].file != rg.nodes[r].file, rid, w, r))
    out.sort(key=lambda x: not x[0])
    return out


def couplings_text(repo, limit=40):
    idx = fresh_index(repo)
    if not idx: return f"No index. Run: vard init {repo}"
    rg = idx["rg"]; pairs = _pairs(idx); lines = [
        f"Implicit data couplings in {os.path.basename(repo)} (writer ⇄ reader through shared state):\n"]
    for cross, rid, w, r in pairs[:limit]:
        wn, rn = rg.nodes[w], rg.nodes[r]
        lines.append(f"[{'⮂ cross-module' if cross else '  same-file'}]  {rid}")
        lines.append(f"     writes: {wn.qual}  ({wn.file}:{wn.start})")
        lines.append(f"     reads : {rn.qual}  ({rn.file}:{rn.start})\n")
    lines.append(f"({len(pairs)} couplings total)")
    return "\n".join(lines)


def context_text(task, repo, k=8, hypothetical=None, runtime_mode=None):
    """hypothetical: optional HyDE snippet — a guess of what the relevant code looks like
    (the calling agent can supply this for free). Bridges the symptom→code vocabulary gap;
    lifts behavioral-issue recall ~+60%. Additive.
    runtime_mode: off/fused/prior/tag — which runtime ranking arm to use (None → auto)."""
    idx = fresh_index(repo)
    if not idx: return f"No index. Run: vard init {repo}"
    rg, res = idx["rg"], idx["res"]
    from . import rank as RK
    from . import selflabel as SL
    rt_mode = RK.resolve_runtime_mode(idx, runtime_mode)
    nodes = [n for n in rg.nodes.values() if n.type in ("function", "method", "class")]
    score, hfiles = RK.rank_nodes(idx, task, repo, nodes, hypothetical=hypothetical,
                                  weights=SL.load_weights(repo),   # per-repo learned weights if present
                                  runtime_mode=rt_mode)
    top = sorted(score, key=score.get, reverse=True)[:k]; topset = set(top)
    fn2res = {}
    for rid in res["nodes"]:
        for f in res["writers"].get(rid, []) + res["readers"].get(rid, []):
            fn2res.setdefault(f, set()).add(rid)
    coupled = {}
    for nid in top:
        for rid in fn2res.get(nid, ()):
            for p in set(res["writers"].get(rid, [])) | set(res["readers"].get(rid, [])):
                if p != nid and p not in topset and not p.endswith("<module>"):
                    coupled.setdefault(p, (rid, nid))
    out = [f"# Context for: {task}\n", "## Directly relevant"]
    for nid in top:
        n = rg.nodes[nid]; out.append(f"- {n.file}:{n.start}-{n.end}  {n.qual}")
    if coupled:
        from . import query as Q
        out.append("\n## Coupled through shared data (grep/embeddings miss these)")
        for pid, (rid, anchor) in list(coupled.items())[:k]:
            n = rg.nodes[pid]
            why = Q.coupling_reason(idx, anchor, pid, rid)
            out.append(f"- {n.file}:{n.start}-{n.end}  {n.qual}   ⮂ {why}")
    # runtime-confirmed: ground truth from `vard test` — code we SAW execute + the REAL call edges among
    # the top hits' neighbors. Agent-uncatchable (it can't run the code); shown as confirmed, not inferred.
    # Hidden in 'off' mode so that arm is a true static baseline for A/B comparison.
    rt_conf = idx.get("rt_confirmed") or set()
    rt_edges = idx.get("rt_edges") or []
    if rt_conf and rt_mode != "off":
        confirmed_top = [nid for nid in top if nid in rt_conf]
        rt_neigh = {}                                        # neighbor -> (direction, anchor) via real call edges
        for ca, ce, _n in rt_edges:
            if ca in topset and ce not in topset:
                rt_neigh.setdefault(ce, ("calls →", ca))
            if ce in topset and ca not in topset:
                rt_neigh.setdefault(ca, ("← called by", ce))
        if confirmed_top or rt_neigh:
            out.append("\n## Confirmed at runtime (observed executing during tests — ground truth)")
            for nid in confirmed_top:
                n = rg.nodes[nid]; out.append(f"- {n.file}:{n.start}-{n.end}  {n.qual}   ✓ ran")
            for pid, (dirn, anchor) in list(rt_neigh.items())[:k]:
                n = rg.nodes[pid]; a = rg.nodes[anchor]
                out.append(f"- {n.file}:{n.start}-{n.end}  {n.qual}   ⮂ {dirn} {a.qual} (real call edge)")
    if hfiles:
        shown = {rg.nodes[nid].file for nid in top}
        histonly = [f for f in hfiles if f not in shown][:k]
        if histonly:
            out.append("\n## Historically associated (similar past changes touched these)")
            for f in histonly:
                out.append(f"- {f}")
    # gated structural expansion: 1-hop import neighbors of the strongest files. High-recall
    # candidates (benchmarks: gated 1-hop recovered the most content-missed gold of any source) —
    # surfaced as labeled candidates, not mixed into the precise hits, since the agent does precision.
    edges = idx.get("import_edges") or []
    if edges:
        from . import propagate as P
        adj = P.undirected_adj(edges)
        shown = {rg.nodes[nid].file for nid in top}
        reach = set()
        for nid in top[:5]:                                  # gate: expand only from strong seeds
            reach |= adj.get(rg.nodes[nid].file, set())
        reach -= shown
        file_best = {}
        for nid, s in score.items():
            fl = rg.nodes[nid].file
            if fl in reach:
                file_best[fl] = max(file_best.get(fl, 0.0), s)
        exp = sorted(file_best, key=file_best.get, reverse=True)[:k]
        if exp:
            out.append("\n## Structurally reachable (imported by/importing relevant code — candidates)")
            for f in exp:
                out.append(f"- {f}")
    # state lineage: the code that DEFINES/PRODUCES the data this task touches. Gated — fires only
    # when state is clearly implicated (a type the task names, or a cache/queue resource it points
    # at), so it stays quiet on ordinary logic bugs. The agent path (vard_state) covers state the
    # task never names by reasoning about it.
    try:
        from . import state as ST
        sg = idx.get("state") or ST.build_state_graph(rg, repo)
        st_types = ST.auto_implicated(sg, rg, task, top)
        if st_types:
            shown = set(top) | set(coupled)
            rows = ST.render(rg, ST.lineage(sg, rg, st_types), exclude=shown)[:k]
            if rows:
                out.append("\n## State lineage (defines/produces the data this task touches)")
                out += rows
    except Exception:
        pass
    # co-located candidates: focused-package siblings of the top hits (content-dark gold is overwhelmingly
    # co-located with a strong hit — a known recall hole). Capped + labeled as candidates; the recall-complete
    # version is the `vard_candidates` pool.
    try:
        import collections as _c
        nodes_all = [n for n in rg.nodes.values() if n.type in ("function", "method", "class")]
        dc = _c.Counter(os.path.dirname(n.file) for n in nodes_all)
        fb = max(80, len(nodes_all) // 150)
        sib_dirs = {os.path.dirname(rg.nodes[t].file) for t in top}
        shown = set(top) | set(coupled)
        sibs = [n.id for n in nodes_all if os.path.dirname(n.file) in sib_dirs
                and dc[os.path.dirname(n.file)] <= fb and n.id not in shown]
        sibs = sorted(sibs, key=lambda c: score.get(c, 0.0), reverse=True)[:k]
        if sibs:
            out.append("\n## Co-located candidates (same focused package as a top hit — candidates)")
            for cid in sibs:
                n = rg.nodes[cid]; out.append(f"- {n.file}:{n.start}-{n.end}  {n.qual}")
    except Exception:
        pass
    # config it depends on: declarative settings the surfaced code reads (@Value/${}/env) — runtime
    # behaviour that's invisible in the code itself.
    try:
        from . import config_index as CFG
        crows = CFG.for_nodes(idx.get("config") or {}, rg, set(top))
        if crows:
            out.append("\n## Config it depends on (runtime settings, not in the code)")
            for r in crows:
                out.append(f"- {r}")
    except Exception:
        pass
    # conversational memory: freshness-verified facts anchored to the code we surfaced (the "why"
    # someone told us, not in the code). Stale-flagged, never asserted silently.
    try:
        from . import memory as MEM
        mtxt = MEM.recall_text(idx, repo, anchors=set(top),
                               files={rg.nodes[nid].file for nid in top}, query=task)
        if mtxt:
            out.append("\n" + mtxt)
    except Exception:
        pass
    return "\n".join(out)


def candidates_text(task, repo):
    """The recall-complete, provenance-tagged candidate pool the agent selects from (recall from the pool,
    precision from the agent). Higher recall than `context` for dark-coupling cases; larger, tagged output."""
    idx = fresh_index(repo)
    if not idx:
        return f"No index. Run: vard init {repo}"
    from . import candidates as CAND
    return CAND.pool_text(idx, task, os.path.abspath(repo))


def config_text(query, repo):
    """Config/properties keys relevant to a query: where each is defined (file:line=value) + the code
    that reads it. Surfaces the runtime settings that change behaviour but aren't in the code."""
    idx = fresh_index(repo)
    if not idx:
        return f"No index. Run: vard init {repo}"
    from . import config_index as CFG
    return CFG.render(idx.get("config") or {}, idx["rg"], query, repo=os.path.abspath(repo))


def remember_text(fact, citations, repo, reason="", kind="mechanism"):
    """Write path: persist a code-anchored fact. citations = comma-separated symbols/files.
    kind ∈ {mechanism, expectation, observation} — which side of the actual-vs-expected join it feeds."""
    idx = fresh_index(repo)
    if not idx:
        return f"No index. Run: vard init {repo}"
    from . import memory as MEM
    cites = [c.strip() for c in citations.split(",") if c.strip()] if isinstance(citations, str) else citations
    r = MEM.remember(idx, os.path.abspath(repo), fact, cites, reason=reason, kind=kind)
    if r.get("stored"):
        return f"✓ remembered ({r['kind']}), anchored to {', '.join(r['anchors'])}  ({r['n_memories']} memories stored)."
    return f"✗ not stored: {r['reason']}  (give a citation that resolves — a symbol name or file:line)"


def expect_text(fact, citations, repo, reason=""):
    """Capture an EXPECTATION (what you expected/intended) — the oracle side of `vard explain`."""
    return remember_text(fact, citations, repo, reason=reason, kind="expectation")


def explain_text(target, repo):
    """The actual-vs-expected JOIN for a symbol/file/ticket — ACTUAL (observed) vs EXPECTED (your
    corrections) with MECHANISM, CONFIG, DIVERGENCE, and UNCERTAINTY, every line provenance-tagged."""
    idx = fresh_index(repo)
    if not idx:
        return f"No index. Run: vard init {repo}"
    from . import memory as MEM
    return MEM.explain(idx, os.path.abspath(repo), target)


def recall_text(task, repo):
    """Read path: fresh, relevant memories for a task. Resolves named symbols as anchors + embedding fallback."""
    idx = fresh_index(repo)
    if not idx:
        return f"No index. Run: vard init {repo}"
    from . import memory as MEM, query as Q
    import re as _re
    rg = idx["rg"]; anchors = set()
    for t in list(set(_re.findall(r'\b[A-Z][A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)?\b', task)))[:10]:
        for i in Q.resolve_target(idx, t)[:3]:
            anchors.add(i)
    files = {rg.nodes[a].file for a in anchors if a in rg.nodes}
    return MEM.recall_text(idx, os.path.abspath(repo), anchors=anchors, files=files, query=task) or "(no relevant memories)"


def state_candidates_text(task, repo):
    """The state types the agent chooses from (the program's data structures, narrowed to the region
    the search surfaced). The agent reads these, reasons about which hold the WRONG state for the
    task, then calls state_lineage with those names."""
    idx = fresh_index(repo)
    if not idx:
        return f"No index. Run: vard init {repo}"
    from . import state as ST, rank as RK, selflabel as SL
    rg = idx["rg"]
    sg = idx.get("state") or ST.build_state_graph(rg, os.path.abspath(repo))
    nodes = ST._content_nodes(rg)
    score, _ = RK.rank_nodes(idx, task, repo, nodes, weights=SL.load_weights(repo))
    seeds = sorted(score, key=score.get, reverse=True)[:10]
    seed_files = {rg.nodes[i].file for i in seeds}
    cands = ST.candidates(sg, rg, seed_files, task)
    # IMPLICATED state (cache/queue/holder types the task points at) goes FIRST: these are the coupling
    # payloads — e.g. the cached DTOs behind a "stale data" bug — that live in far modules and would be
    # dropped by the seed-proximity narrowing. This is what lets the agent reach dark coupling state.
    imp = sorted(ST.auto_implicated(sg, rg, task, set(seeds)))
    cands = imp + [c for c in cands if c not in set(imp)]
    return ("# Candidate state types for: " + (task or "")[:120] + "\n"
            "# The first types are IMPLICATED by the task (cache/queue/shared-state it points at) — the most "
            "likely coupling payloads. Identify which hold/define the WRONG state (or that the fix must "
            "change), including state the task does NOT name but is structurally involved, then call "
            "vard_state_lineage with those type names.\n" + ", ".join(cands))


def state_lineage_text(types, repo):
    """Given state type names (e.g. the ones the agent identified), return the code that defines and
    produces/consumes them: the type defs + their members + producers/consumers + interface/impl."""
    idx = fresh_index(repo)
    if not idx:
        return f"No index. Run: vard init {repo}"
    from . import state as ST
    rg = idx["rg"]
    sg = idx.get("state") or ST.build_state_graph(rg, os.path.abspath(repo))
    if isinstance(types, str):
        types = [t.strip() for t in re.split(r"[,\s]+", types) if t.strip()]
    ids = ST.lineage(sg, rg, types)
    rows = ST.render(rg, ids)
    if not rows:
        known = ", ".join(sorted(sg["type_def"])[:20])
        return f"no lineage for {types}. State types include e.g.: {known}"
    return f"# State lineage for {types}\n" + "\n".join(rows)


def whole_picture_text(target, repo):
    """The relational WHOLE PICTURE for a file/symbol: code + state it touches + coupled state +
    the decisions/tickets/incidents behind it (from history) + what co-changes with it. The context
    an agent can't reconstruct from code alone."""
    idx = fresh_index(repo)
    if not idx:
        return f"No index. Run: vard init {repo}"
    from . import memory as MEM
    return MEM.whole_picture(idx, os.path.abspath(repo), target)


def impact_text(target, repo):
    idx = fresh_index(repo)
    if not idx:
        return f"No index. Run: vard init {repo}"
    from . import query as Q
    r = Q.impact(idx, target)
    if r.get("error"):
        return f"impact: {r['error']} for '{target}'"
    out = [f"# Impact of changing: {', '.join(r['target'])}"]
    if r["writes"]:
        out.append(f"  writes: {', '.join(r['writes'])}")
    if r["reads"]:
        out.append(f"  reads : {', '.join(r['reads'])}")
    if not r["items"]:
        out.append("\n(no coupled code found — safe / isolated)")
        return "\n".join(out)
    out.append(f"\n## Affected ({r['n']} found, showing {len(r['items'])}) — review before editing:")
    cur = None
    for it in r["items"]:
        if it["relation"] != cur:
            cur = it["relation"]; out.append(f"\n[{cur}]")
        out.append(f"  {it['loc']}  {it['qual']}")
        out.append(f"      ↳ {it['reason']}")
    return "\n".join(out)


def resource_text(name, repo):
    idx = fresh_index(repo)
    if not idx:
        return f"No index. Run: vard init {repo}"
    from . import query as Q
    r = Q.resource(idx, name)
    if not r["resources"]:
        return f"No resource matching '{name}'."
    out = [f"# Resources matching '{name}' ({r['n_matched']} matched)"]
    for res in r["resources"]:
        out.append(f"\n{res['resource']}")
        for w in res["writers"]:
            out.append(f"   ✎ writes  {w['loc']}  {w['qual']}")
        for rd in res["readers"]:
            out.append(f"   ◇ reads   {rd['loc']}  {rd['qual']}")
    return "\n".join(out)


def _hook_command():
    import shutil
    exe = shutil.which("vard-hook")
    return exe if exe else f'"{sys.executable}" -m vard.hook'


def install_hook(scope, repo):
    """Merge the PreToolUse Edit|Write hook into a settings.json (idempotent)."""
    import json as _j
    path = (os.path.expanduser("~/.claude/settings.json") if scope == "global"
            else os.path.join(os.path.abspath(repo), ".claude", "settings.json"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data = {}
    if os.path.isfile(path):
        try: data = _j.load(open(path))
        except Exception: data = {}
    cmd = _hook_command()
    hooksroot = data.setdefault("hooks", {})

    def _present(event):
        for entry in hooksroot.get(event, []):
            for h in entry.get("hooks", []):
                c = h.get("command") or ""
                if "vard" in c and "hook" in c:
                    return True
        return False

    added = []
    if not _present("PreToolUse"):                       # blast-radius warning on edits
        hooksroot.setdefault("PreToolUse", []).append(
            {"matcher": "Edit|Write", "hooks": [{"type": "command", "command": cmd}]})
        added.append("PreToolUse (impact)")
    if not _present("UserPromptSubmit"):                 # memory recall-inject + capture
        hooksroot.setdefault("UserPromptSubmit", []).append(
            {"hooks": [{"type": "command", "command": cmd}]})
        added.append("UserPromptSubmit (memory)")
    if not added:
        return f"VARD hooks already installed in {path}"
    _j.dump(data, open(path, "w"), indent=2)
    return (f"✓ installed VARD hooks → {path}\n  command: {cmd}\n  events: {', '.join(added)}\n"
            f"  (silent unless the repo is `vard init`'d)")


def uninstall_hook(scope, repo):
    import json as _j
    path = (os.path.expanduser("~/.claude/settings.json") if scope == "global"
            else os.path.join(os.path.abspath(repo), ".claude", "settings.json"))
    if not os.path.isfile(path):
        return f"no settings at {path}"
    data = _j.load(open(path))
    isvard = lambda h: "vard" in (h.get("command") or "") and "hook" in (h.get("command") or "")
    for ev in ("PreToolUse", "UserPromptSubmit"):
        entries = data.get("hooks", {}).get(ev)
        if entries is not None:
            data["hooks"][ev] = [e for e in entries if not any(isvard(h) for h in e.get("hooks", []))]
    _j.dump(data, open(path, "w"), indent=2)
    return f"✓ removed VARD hooks from {path}"


def _agent_jar(override=None):
    """Locate the bundled JVM agent jar. Editable installs ship it at <repo>/vard-agent/vard-agent.jar."""
    if override:
        return os.path.abspath(override) if os.path.isfile(override) else None
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # .../vard (project root)
    for cand in (os.path.join(here, "vard-agent", "vard-agent.jar"),
                 os.path.join(os.path.dirname(here), "vard-agent", "vard-agent.jar")):
        if os.path.isfile(cand):
            return cand
    return None


def _derive_java_pkgs(idx, repo):
    """Top-2-segment package prefixes from the repo's Java sources (e.g. com.example) — the sampler keeps
    only these classes, so the trace is app code, not the test framework / maven internals."""
    rg = idx["rg"]; root = os.path.abspath(repo); prefixes = set()
    for f in {n.file for n in rg.nodes.values() if n.file.endswith(".java")}:
        try:
            txt = open(os.path.join(root, f), encoding="utf-8", errors="ignore").read(2000)
        except Exception:
            continue
        m = re.search(r"^[ \t]*package[ \t]+([\w.]+)[ \t]*;", txt, re.M)
        if m:
            segs = m.group(1).split(".")
            prefixes.add(".".join(segs[:2]) if len(segs) >= 2 else segs[0])
    return sorted(prefixes)


def run_test(repo, command, jar=None, pkgs=None, ms="2", env=None):
    """`vard test -- <cmd>`: run the dev's existing test command (default `mvn test`) under the VARD java
    agent, then merge the ground-truth trace (what actually executed + real call edges) into the runtime
    overlay. The dev runs the tests; VARD only listens — no app to stand up, it just needs to compile."""
    import subprocess, glob
    repo = _project_root(repo)
    jarp = _agent_jar(jar)
    if not jarp:
        return ("vard test: agent jar not found. Build it:\n"
                "  bash vard-agent/build.sh        # targets JDK 11 bytecode (loads on 11 and 17)\n"
                "  (or pass --jar <path>)")
    print("→ vard: ensuring the index is fresh before the run...", file=sys.stderr)
    idx = fresh_index(repo)
    if not idx:
        return f"No index. Run: vard init {repo}"
    if pkgs is None:
        pkgs = ",".join(_derive_java_pkgs(idx, repo))
    cmd = command or ["mvn", "test"]
    runenv = env or "test"                                # provenance label for this run (see per-env overlay)
    tracedir = os.path.join(os.path.abspath(repo), VDIR, "trace")
    if os.path.isdir(tracedir):
        for old in glob.glob(os.path.join(tracedir, "*")):
            try: os.remove(old)
            except OSError: pass
    os.makedirs(tracedir, exist_ok=True)
    outbase = os.path.join(tracedir, "run.jsonl")
    # JAVA_TOOL_OPTIONS reaches EVERY JVM the build spawns — including the JVM Surefire forks for the actual
    # tests (which -javaagent via MAVEN_OPTS would miss). The agent writes one per-PID file per JVM.
    jto = (f"-javaagent:{jarp} -Dvard.out={outbase} -Dvard.ms={ms} -Dvard.env={runenv}"
           + (f" -Dvard.pkgs={pkgs}" if pkgs else ""))
    jenv = dict(os.environ)
    jenv["JAVA_TOOL_OPTIONS"] = (jenv.get("JAVA_TOOL_OPTIONS", "") + " " + jto).strip()
    print(f"→ vard: running `{' '.join(cmd)}` under the agent (env={runenv}, pkgs: {pkgs or 'all app classes'})...", file=sys.stderr)
    try:
        rc = subprocess.run(cmd, cwd=os.path.abspath(repo), env=jenv).returncode
    except FileNotFoundError:
        return f"vard test: command not found: {cmd[0]}"
    return _ingest_traces(idx, repo, tracedir, env=runenv,
                          header=f"✓ vard test: exit {rc}; ", clean=True)


def _ingest_traces(idx, repo, tracedir, env, header, clean):
    """Merge every per-PID trace file in `tracedir` into the runtime overlay under `env`. Shared by
    `vard test` and `vard attach`."""
    import glob
    from . import runtime as RT
    traces = sorted(glob.glob(os.path.join(tracedir, "run.jsonl.*")))
    if not traces:
        return (header + "no trace captured. The target JVM may not have honored the agent, or no app "
                "classes (pkgs filter) executed.")
    tot = {"methods": 0, "edges": 0, "values": 0, "unresolved": 0, "runs": []}
    for tp in traces:
        r = RT.ingest(idx, repo, tp, env=env)
        if r.get("ok"):
            tot.update(methods=r["methods"], edges=r["edges"], values=r["values"], runs=r["runs"])
            tot["unresolved"] += r.get("unresolved", 0)
    if clean:
        for tp in traces:
            try: os.remove(tp)
            except OSError: pass
    return (header + f"merged {len(traces)} JVM trace(s) into env='{env}' → runtime overlay\n"
            f"  {tot['methods']} methods confirmed live, {tot['edges']} real call edges, {tot['values']} with values"
            + (f"  ({tot['unresolved']} unmatched)" if tot["unresolved"] else "")
            + f"\n  runs in overlay: {', '.join(tot['runs'])}"
            + f"\n  → context/impact/explain now use these as runtime-confirmed (ground truth).")


def attach_run(pid, repo, jar=None, pkgs=None, env="local", for_secs=30, flush=5):
    """`vard attach <pid>`: load the agent into an ALREADY-RUNNING JVM (no restart), let it run for `for_secs`
    while you exercise it (the agent flushes a trace every `flush`s), then merge what it observed under `env`.
    The way to ground ACTUAL against a live local/staging server, not just the test suite."""
    import subprocess, glob
    repo = _project_root(repo)
    jarp = _agent_jar(jar)
    if not jarp:
        return "vard attach: agent jar not found — build it with `bash vard-agent/build.sh` (or pass --jar)."
    idx = fresh_index(repo)
    if not idx:
        return f"No index. Run: vard init {repo}"
    if pkgs is None:
        pkgs = ",".join(_derive_java_pkgs(idx, repo))
    tracedir = os.path.join(os.path.abspath(repo), VDIR, "trace")
    for old in glob.glob(os.path.join(tracedir, "*")):
        try: os.remove(old)
        except OSError: pass
    os.makedirs(tracedir, exist_ok=True)
    outbase = os.path.join(tracedir, "run.jsonl")
    # agentmain can't read -D on a running JVM, so options ride in the loadAgent args string (k=v;...)
    agent_args = f"out={outbase};values=*;env={env};flush={flush}" + (f";pkgs={pkgs}" if pkgs else "")
    java = os.path.join(os.environ["JAVA_HOME"], "bin", "java") if os.environ.get("JAVA_HOME") else "java"
    print(f"→ vard: attaching agent to pid {pid} (env={env}, flush={flush}s)...", file=sys.stderr)
    try:
        a = subprocess.run([java, "-cp", jarp, "vard.agent.Attacher", str(pid), jarp, agent_args],
                           capture_output=True, text=True)
    except FileNotFoundError:
        return "vard attach: `java` not found on PATH (set JAVA_HOME or add java to PATH)."
    if a.returncode != 0:
        return (f"vard attach: failed to attach to pid {pid}.\n  {(a.stderr or a.stdout).strip()[:300]}\n"
                "  (the target must be a JVM you own; same-user only.)")
    import time
    print(f"→ vard: attached. Exercise the app now — observing for {for_secs}s...", file=sys.stderr)
    time.sleep(for_secs)
    return _ingest_traces(idx, repo, tracedir, env=env,
                          header=f"✓ vard attach: pid {pid} observed {for_secs}s; ", clean=True)


def main():
    ap = argparse.ArgumentParser(prog="vard")
    sub = ap.add_subparsers(dest="cmd", required=True)
    pi = sub.add_parser("init"); pi.add_argument("repo", nargs="?", default="."); pi.add_argument("--fresh", action="store_true")
    pi.add_argument("--no-wire", action="store_true", help="just index; don't touch CLAUDE.md or register the MCP server")
    pi.add_argument("--with", dest="with_roots", action="append", default=[], metavar="PATH",
                    help="extra source root to index (e.g. a dependency module/repo outside the tree); repeatable")
    pi.add_argument("--deps", action="store_true", help="auto-discover + index co-located source dependencies")
    pc = sub.add_parser("couplings"); pc.add_argument("repo", nargs="?", default="."); pc.add_argument("--limit", type=int, default=40)
    px = sub.add_parser("context"); px.add_argument("task"); px.add_argument("repo", nargs="?", default="."); px.add_argument("-k", type=int, default=8); px.add_argument("--hypothetical", default=None)
    px.add_argument("--runtime", dest="runtime_mode", default=None, choices=["off", "fused", "prior", "tag"],
                    help="runtime ranking arm (default: auto — fused if a trace exists, else off)")
    pca = sub.add_parser("candidates"); pca.add_argument("task"); pca.add_argument("repo", nargs="?", default=".")
    pm = sub.add_parser("impact"); pm.add_argument("target"); pm.add_argument("repo", nargs="?", default=".")
    pr = sub.add_parser("resource"); pr.add_argument("name"); pr.add_argument("repo", nargs="?", default=".")
    pw = sub.add_parser("whole-picture"); pw.add_argument("target"); pw.add_argument("repo", nargs="?", default=".")
    psc = sub.add_parser("state-candidates"); psc.add_argument("task"); psc.add_argument("repo", nargs="?", default=".")
    psl = sub.add_parser("state-lineage"); psl.add_argument("types"); psl.add_argument("repo", nargs="?", default=".")
    prm = sub.add_parser("remember"); prm.add_argument("fact"); prm.add_argument("citations",
        help="comma-separated code anchors (symbol or file:line) the fact is about")
    prm.add_argument("repo", nargs="?", default="."); prm.add_argument("--reason", default="")
    prm.add_argument("--kind", default="mechanism", choices=["mechanism", "expectation", "observation"],
                     help="which side of the actual-vs-expected join this feeds")
    pex = sub.add_parser("expect", help="capture an EXPECTATION (oracle side of `vard explain`)")
    pex.add_argument("fact"); pex.add_argument("citations",
        help="comma-separated code anchors the expectation is about")
    pex.add_argument("repo", nargs="?", default="."); pex.add_argument("--reason", default="")
    pxp = sub.add_parser("explain", help="actual-vs-expected JOIN for a symbol/file/ticket (provenance-tagged)")
    pxp.add_argument("target"); pxp.add_argument("repo", nargs="?", default=".")
    prc = sub.add_parser("recall"); prc.add_argument("task"); prc.add_argument("repo", nargs="?", default=".")
    pcf = sub.add_parser("config"); pcf.add_argument("query"); pcf.add_argument("repo", nargs="?", default=".")
    ph = sub.add_parser("install-hook"); ph.add_argument("repo", nargs="?", default="."); ph.add_argument("--global", dest="glob", action="store_true")
    pu = sub.add_parser("uninstall-hook"); pu.add_argument("repo", nargs="?", default="."); pu.add_argument("--global", dest="glob", action="store_true")
    pl = sub.add_parser("learn"); pl.add_argument("repo", nargs="?", default="."); pl.add_argument("--sample", type=int, default=150)
    ptt = sub.add_parser("test", help="run the test suite under the JVM agent → merge ground-truth runtime trace")
    ptt.add_argument("--repo", default="."); ptt.add_argument("--jar", default=None, help="path to vard-agent.jar")
    ptt.add_argument("--pkgs", default=None, help="comma class-prefixes to trace (default: auto from repo)")
    ptt.add_argument("--ms", default="2", help="stack-sampling interval (ms; sampler fallback only)")
    ptt.add_argument("--env", default="test", help="provenance label for this run (e.g. test, local, staging)")
    ptt.add_argument("command", nargs=argparse.REMAINDER, help="command after -- (default: mvn test)")
    pat = sub.add_parser("attach", help="attach the agent to an ALREADY-RUNNING JVM (no restart) + observe")
    pat.add_argument("pid", help="target JVM process id (same user)")
    pat.add_argument("--repo", default="."); pat.add_argument("--jar", default=None)
    pat.add_argument("--pkgs", default=None, help="class-prefixes to trace (default: auto from repo)")
    pat.add_argument("--env", default="local", help="provenance label (e.g. local, staging, prod)")
    pat.add_argument("--for", dest="for_secs", type=int, default=30, help="seconds to observe before merging")
    pat.add_argument("--flush", type=int, default=5, help="agent trace-flush interval (seconds)")
    pru = sub.add_parser("rules", help="print (or --write) agent routing rules for CLAUDE.md / AGENTS.md")
    pru.add_argument("repo", nargs="?", default="."); pru.add_argument("--write", action="store_true")
    pru.add_argument("--file", default=None, help="target file (default: existing CLAUDE.md/AGENTS.md, else CLAUDE.md)")
    a = ap.parse_args()
    try:
        _dispatch(a)
    except KeyboardInterrupt:
        print("\nvard: interrupted", file=sys.stderr); sys.exit(130)
    except Exception as e:
        # never dump a raw traceback at a user; one clean line + opt-in detail
        if os.environ.get("VARD_DEBUG"):
            raise
        print(f"vard: {type(e).__name__}: {str(e)[:200]}\n  (re-run with VARD_DEBUG=1 for the full traceback)", file=sys.stderr)
        sys.exit(1)


def _dispatch(a):
    if a.cmd == "init":
        extra = list(getattr(a, "with_roots", []) or [])
        if getattr(a, "deps", False):
            try:
                from . import deps as DEP
                found = DEP.discover_source_deps(_project_root(a.repo))
                if found:
                    print(f"  • found {len(found)} co-located source dep(s): "
                          + ", ".join(os.path.basename(d) for d in found))
                extra += found
            except Exception as e:
                print(f"  • dep discovery skipped ({str(e)[:40]})")
        s = build_index(a.repo, a.fresh, extra_roots=extra)
        print(f"✓ indexed {s['repo']}  (ruleset: {s['ruleset_source']})")
        print(f"  code: {s['code_nodes']} nodes / {s['code_edges']} edges")
        print(f"  resources: {s['resources']}, {s['resource_edges']} read/write edges")
        if not a.no_wire:
            print(f"  • {_write_rules(a.repo)}")               # agent uses VARD automatically
            m = _wire_mcp(a.repo)
            if m:
                print(f"  • {m}")
            print("  → done. Just describe a task to your agent — it'll use VARD on its own.")
    elif a.cmd == "rules":
        print(_write_rules(a.repo, a.file) if a.write else AGENT_RULES)
    elif a.cmd == "couplings":
        print(couplings_text(a.repo, a.limit))
    elif a.cmd == "context":
        print(context_text(a.task, a.repo, a.k, a.hypothetical, runtime_mode=a.runtime_mode))
    elif a.cmd == "candidates":
        print(candidates_text(a.task, a.repo))
    elif a.cmd == "impact":
        print(impact_text(a.target, a.repo))
    elif a.cmd == "resource":
        print(resource_text(a.name, a.repo))
    elif a.cmd == "whole-picture":
        print(whole_picture_text(a.target, a.repo))
    elif a.cmd == "state-candidates":
        print(state_candidates_text(a.task, a.repo))
    elif a.cmd == "state-lineage":
        print(state_lineage_text(a.types, a.repo))
    elif a.cmd == "remember":
        print(remember_text(a.fact, a.citations, a.repo, reason=a.reason, kind=a.kind))
    elif a.cmd == "expect":
        print(expect_text(a.fact, a.citations, a.repo, reason=a.reason))
    elif a.cmd == "explain":
        print(explain_text(a.target, a.repo))
    elif a.cmd == "recall":
        print(recall_text(a.task, a.repo))
    elif a.cmd == "config":
        print(config_text(a.query, a.repo))
    elif a.cmd == "install-hook":
        print(install_hook("global" if a.glob else "project", a.repo))
    elif a.cmd == "uninstall-hook":
        print(uninstall_hook("global" if a.glob else "project", a.repo))
    elif a.cmd == "test":
        cmd = [c for c in (a.command or []) if c != "--"]
        print(run_test(a.repo, cmd, jar=a.jar, pkgs=a.pkgs, ms=a.ms, env=a.env))
    elif a.cmd == "attach":
        print(attach_run(a.pid, a.repo, jar=a.jar, pkgs=a.pkgs, env=a.env, for_secs=a.for_secs, flush=a.flush))
    elif a.cmd == "learn":
        idx = fresh_index(a.repo)
        if not idx:
            print(f"No index. Run: vard init {a.repo}"); return
        try:
            import sklearn  # noqa: F401
        except Exception:
            print("learn: needs scikit-learn — install it:  pip install 'vard[learn]'"); return
        from . import selflabel as SL
        w = SL.learn(a.repo, idx, sample=a.sample)
        if not w:
            print("learn: insufficient or non-predictive git history (need ≥10 usable commits whose\n"
                  "  messages relate to the files they touch) — keeping the benchmark-tuned defaults")
        else:
            m = w.get("_meta", {})
            print(f"✓ learned per-repo ranking weights from {m.get('n_commits','?')} commits "
                  f"({m.get('n_pos','?')} positives / {m.get('n_examples','?')} examples)")
            print(f"  history={w['history']:.2f}  ppr={w['ppr']:.2f}  (relative to content≙1.0; defaults were 0.60 / 0.45)")
            print(f"  saved → {SL.weights_path(a.repo)}")


if __name__ == "__main__":
    main()
