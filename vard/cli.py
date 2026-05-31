#!/usr/bin/env python3
"""
vard — repository attention for AI coding agents (stack-agnostic, key-optional).

  vard init <repo>                 # discover + index (run once)
  vard couplings <repo>            # list implicit data-coupling (writer <-> reader)
  vard context "<task>" <repo>     # retrieve relevant code + coupled partners

Shared core functions (build_index / couplings_text / context_text) are reused by
the MCP server so an agent can call the same logic.
"""
import argparse, os, pickle, sys
from . import common as C, graph as G, resources as R, freshness as Fr

VDIR = ".vard"


def _index_path(repo): return os.path.join(os.path.abspath(repo), VDIR, "index.pkl")


def build_index(repo, fresh=False, llm=None):
    """Build & cache the attention graph + resource layer. llm: optional agent LLM for discovery."""
    repo = os.path.abspath(repo)
    os.makedirs(os.path.join(repo, VDIR), exist_ok=True)
    try:
        from . import discover as D
        rs = D.discover(repo, use_cache=not fresh, llm=llm)
        src = "agent" if llm else ("openai" if os.environ.get("VARD_DISCOVER", "").lower() == "openai" else "default")
    except Exception as e:
        rs, src = R.DEFAULT_RULESET, f"default ({str(e)[:40]})"
    print("→ vard: parsing source files (tree-sitter)...", file=sys.stderr, flush=True)
    rg = G.build_graph(repo)            # multi-language (python/java/js/ts/go)
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
        with open(_index_path(repo), "wb") as f:
            pickle.dump({"rg": rg, "ruleset": rs, "fingerprint": Fr.fingerprint(repo), "history": hist,
                         "import_edges": import_edges, "res": res}, f)
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
    idx = load_index(repo)
    cur = Fr.fingerprint(repo)
    if idx is None:
        print("→ vard: building index (first run)...", file=sys.stderr)
        build_index(repo); return load_index(repo)
    if not Fr.is_fresh(idx.get("fingerprint", {}), cur):
        changed, deleted = Fr.diff(idx.get("fingerprint", {}), cur)
        print(f"→ vard: repo changed ({len(changed)} modified, {len(deleted)} removed) — re-indexing...", file=sys.stderr)
        build_index(repo); return load_index(repo)
    return idx   # unchanged → instant


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


def context_text(task, repo, k=8, hypothetical=None):
    """hypothetical: optional HyDE snippet — a guess of what the relevant code looks like
    (the calling agent can supply this for free). Bridges the symptom→code vocabulary gap;
    lifts behavioral-issue recall ~+60%. Additive."""
    idx = fresh_index(repo)
    if not idx: return f"No index. Run: vard init {repo}"
    rg, res = idx["rg"], idx["res"]
    from . import rank as RK
    from . import selflabel as SL
    nodes = [n for n in rg.nodes.values() if n.type in ("function", "method", "class")]
    score, hfiles = RK.rank_nodes(idx, task, repo, nodes, hypothetical=hypothetical,
                                  weights=SL.load_weights(repo))   # per-repo learned weights if present
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
    return "\n".join(out)


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
    hooks = data.setdefault("hooks", {}).setdefault("PreToolUse", [])
    for entry in hooks:                                  # idempotent: skip if already present
        for h in entry.get("hooks", []):
            if "vard" in (h.get("command") or "") and "hook" in (h.get("command") or ""):
                return f"VARD hook already installed in {path}"
    hooks.append({"matcher": "Edit|Write", "hooks": [{"type": "command", "command": cmd}]})
    _j.dump(data, open(path, "w"), indent=2)
    return f"✓ installed VARD pre-edit impact hook → {path}\n  command: {cmd}\n  (fires on Edit/Write; silent unless the repo is `vard init`'d)"


def uninstall_hook(scope, repo):
    import json as _j
    path = (os.path.expanduser("~/.claude/settings.json") if scope == "global"
            else os.path.join(os.path.abspath(repo), ".claude", "settings.json"))
    if not os.path.isfile(path):
        return f"no settings at {path}"
    data = _j.load(open(path))
    pre = data.get("hooks", {}).get("PreToolUse", [])
    kept = [e for e in pre if not any("vard" in (h.get("command") or "") and "hook" in (h.get("command") or "")
                                       for h in e.get("hooks", []))]
    data.get("hooks", {})["PreToolUse"] = kept
    _j.dump(data, open(path, "w"), indent=2)
    return f"✓ removed VARD hook from {path}"


def main():
    ap = argparse.ArgumentParser(prog="vard")
    sub = ap.add_subparsers(dest="cmd", required=True)
    pi = sub.add_parser("init"); pi.add_argument("repo", nargs="?", default="."); pi.add_argument("--fresh", action="store_true")
    pc = sub.add_parser("couplings"); pc.add_argument("repo", nargs="?", default="."); pc.add_argument("--limit", type=int, default=40)
    px = sub.add_parser("context"); px.add_argument("task"); px.add_argument("repo", nargs="?", default="."); px.add_argument("-k", type=int, default=8); px.add_argument("--hypothetical", default=None)
    pm = sub.add_parser("impact"); pm.add_argument("target"); pm.add_argument("repo", nargs="?", default=".")
    pr = sub.add_parser("resource"); pr.add_argument("name"); pr.add_argument("repo", nargs="?", default=".")
    ph = sub.add_parser("install-hook"); ph.add_argument("repo", nargs="?", default="."); ph.add_argument("--global", dest="glob", action="store_true")
    pu = sub.add_parser("uninstall-hook"); pu.add_argument("repo", nargs="?", default="."); pu.add_argument("--global", dest="glob", action="store_true")
    pl = sub.add_parser("learn"); pl.add_argument("repo", nargs="?", default="."); pl.add_argument("--sample", type=int, default=150)
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
        s = build_index(a.repo, a.fresh)
        print(f"✓ indexed {s['repo']}  (ruleset: {s['ruleset_source']})")
        print(f"  code: {s['code_nodes']} nodes / {s['code_edges']} edges")
        print(f"  resources: {s['resources']}, {s['resource_edges']} read/write edges")
    elif a.cmd == "couplings":
        print(couplings_text(a.repo, a.limit))
    elif a.cmd == "context":
        print(context_text(a.task, a.repo, a.k, a.hypothetical))
    elif a.cmd == "impact":
        print(impact_text(a.target, a.repo))
    elif a.cmd == "resource":
        print(resource_text(a.name, a.repo))
    elif a.cmd == "install-hook":
        print(install_hook("global" if a.glob else "project", a.repo))
    elif a.cmd == "uninstall-hook":
        print(uninstall_hook("global" if a.glob else "project", a.repo))
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
