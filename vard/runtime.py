#!/usr/bin/env python3
"""Runtime layer — the highest-quality signal. The dev runs their test suite under VARD's java agent
(`vard test` → `mvn test -javaagent:vard-agent.jar`); the agent emits a JSONL trace of what ACTUALLY
executed (methods + real caller→callee edges). This is ground truth a static reader cannot reconstruct
(resolves dynamic dispatch / interface→impl, confirms which code is live). We persist it (like memory),
freshness-anchor it to the code (re-hash on read; stale if the method changed), and overlay it on the
graph as the top-confidence tier. Coverage = whatever the tests exercised; it accretes over runs. Static
stays the recall floor; runtime CONFIRMS and CORRECTS the exercised subset.

Resolution note: the JVM reports fully-qualified names (`com.example.FareService.computeFare`,
nested as `Outer$Inner.method`), but VARD's tree-sitter node quals drop the package (`FareService.computeFare`,
`Outer.Inner.method`). We bridge that here by matching a trace qual against the LONGEST node qual that is a
dotted suffix of it — so it works on real packaged Maven repos, not only the package-less demo.
"""
import json, os
from . import memory as MEM


def _path(repo):
    return os.path.join(os.path.abspath(repo), ".vard", "runtime.json")


def _qual_index(rg):
    """node qual -> [node ids]. Only callable code (methods/functions/classes); modules are skipped."""
    idx = {}
    for nid, n in rg.nodes.items():
        if n.type in ("function", "method", "class"):
            idx.setdefault(n.qual, []).append(nid)
    return idx


_LAMBDA = __import__("re").compile(r"lambda\$(\w+)\$\d+")


def _resolve_qual(qual2ids, trace_qual):
    """Map a JVM trace qual to a node id. Attribute synthetic lambda bodies to their ENCLOSING method
    (`Foo.lambda$score$0` → `Foo.score` — a lambda running means its method ran), normalize nested-class
    `$`→`.`, then try dotted suffixes of the FQN from longest to shortest, returning the first (longest,
    most specific) that hits a node. This collapses most of the 'unresolved' bucket, which is dominated by
    lambdas/synthetic methods, and correctly credits the real method."""
    q = _LAMBDA.sub(r"\1", trace_qual).replace("$", ".")
    parts = q.split(".")
    for i in range(len(parts)):                      # i=0 → full FQN, then drop leading segments
        cand = ".".join(parts[i:])
        ids = qual2ids.get(cand)
        if ids and len(ids) == 1:                    # unambiguous suffix match wins
            return ids[0]
    # fall back: a shorter suffix that resolves even if ambiguous (take the first deterministically)
    for i in range(len(parts)):
        ids = qual2ids.get(".".join(parts[i:]))
        if ids:
            return sorted(ids)[0]
    return None


def _add_env(envs, env, n):
    envs[env] = envs.get(env, 0) + int(n)


def ingest(idx, repo, trace_path, env=None):
    """Parse the agent's JSONL trace, resolve method quals to VARD nodes, persist runtime-confirmed methods +
    real call edges + observed values, each TAGGED with the env that produced it. Accretes: merges with any
    prior runtime.json (an observation seen in any run stays until its code changes), but keeps per-env counts
    so a merged overlay never conflates a test-path run with a prod/local one. Effective env =
    explicit `env` arg > the trace's own env label > its active profile > 'default'."""
    rg = idx["rg"]
    q2i = _qual_index(rg)
    prior = load(repo)
    methods = {m["anchor"]: m for m in prior.get("methods", []) if m["anchor"] in rg.nodes}
    edgeset = {(e["caller"], e["callee"]): e for e in prior.get("edges", [])
               if e["caller"] in rg.nodes and e["callee"] in rg.nodes}
    valuemap = {a: {s["v"]: dict(s.get("envs", {})) for s in samples}     # anchor -> {sample-string -> {env:n}}
                for a, samples in prior.get("values", {}).items() if a in rg.nodes}
    # mutations keyed by tuple → {fields, envs}. The writer node qual is resolved to a node id where possible.
    mutmap = {}
    for mu in prior.get("mutations", []):
        mutmap[(mu.get("anchor") or mu.get("node"), mu.get("kind"), mu.get("target"),
                mu.get("op"), mu.get("before"), mu.get("after"))] = dict(mu.get("envs", {}))
    runs = dict(prior.get("runs", {}))
    instrumented = set(prior.get("instrumented_classes", []))    # classes the agent transformed (coverage)
    try:
        with open(trace_path, encoding="utf-8", errors="ignore") as _f:
            lines = [l.strip() for l in _f if l.strip()]
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}
    def _loads(s):
        try:
            o = json.loads(s)
            return o if isinstance(o, dict) else None
        except Exception:
            return None
    # resolve the effective env BEFORE tagging observations (the config record carries env+profile+mode)
    cfg = next((c for c in (_loads(l) for l in lines if '"t":"config"' in l.replace(" ", "")) if c), {})
    eff_env = (str(env or cfg.get("env") or cfg.get("profile") or "default")).strip() or "default"
    runs[eff_env] = {"profile": cfg.get("profile", ""), "mode": cfg.get("mode", "sample")}
    unresolved = 0
    unresolved_quals = {}                                    # qual -> count, for a diagnostic histogram
    def _miss(qual):
        nonlocal unresolved
        unresolved += 1
        if len(unresolved_quals) < 500:
            unresolved_quals[qual] = unresolved_quals.get(qual, 0) + 1
    try:
        for line in lines:
            o = _loads(line)                                 # one malformed line must not drop the whole trace
            if o is None:
                continue
            t = o.get("t")
            if t == "method":
                nid = _resolve_qual(q2i, o["qual"])
                if not nid:
                    _miss(o.get("qual", "?")); continue
                n = rg.nodes[nid]
                m = methods.get(nid) or {"anchor": nid, "file": n.file, "qual": n.qual, "envs": {}}
                m.setdefault("envs", {})
                _add_env(m["envs"], eff_env, o.get("hits", 0))
                m["hits"] = sum(m["envs"].values())
                m["hash"] = MEM._anchor_hash(idx, repo, nid)
                methods[nid] = m
            elif t == "edge":
                ca, ce = _resolve_qual(q2i, o["caller"]), _resolve_qual(q2i, o["callee"])
                if not (ca and ce) or ca == ce:
                    continue
                e = edgeset.get((ca, ce)) or {"caller": ca, "callee": ce, "envs": {}}
                e.setdefault("envs", {})
                _add_env(e["envs"], eff_env, o.get("n", 1))
                e["n"] = sum(e["envs"].values())
                edgeset[(ca, ce)] = e
            elif t == "value":
                nid = _resolve_qual(q2i, o["qual"])
                if not nid:
                    _miss(o.get("qual", "?")); continue
                bucket = valuemap.setdefault(nid, {})
                for s in o.get("samples", []):
                    _add_env(bucket.setdefault(s["v"], {}), eff_env, s.get("n", 1))
            elif t == "mutation":                            # observed state change (write/update/del/read)
                anchor = _resolve_qual(q2i, o.get("node", ""))   # may be None if the writer isn't an indexed node
                k = (anchor, o.get("kind"), o.get("target"), o.get("op"),
                     o.get("before") or None, o.get("after") or None)
                _add_env(mutmap.setdefault(k, {}), eff_env, o.get("n", 1))
            elif t == "class":                               # instrumented-class set (for coverage diagnosis)
                if o.get("name"):
                    instrumented.add(o["name"])
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}
    # cap stored value samples per method (most-observed first) so the overlay can't bloat
    values_out = {a: [{"v": v, "n": sum(envs.values()), "envs": envs}
                      for v, envs in sorted(b.items(), key=lambda kv: -sum(kv[1].values()))[:8]]
                  for a, b in valuemap.items()}
    mutations_out = [{"anchor": k[0], "kind": k[1], "target": k[2], "op": k[3], "before": k[4],
                      "after": k[5], "n": sum(envs.values()), "envs": envs}
                     for k, envs in sorted(mutmap.items(), key=lambda kv: -sum(kv[1].values()))[:1000]]
    # histogram unresolved quals by class prefix (drop the method segment) so the user can see WHAT missed —
    # a big lambda/synthetic/generated bucket is expected; real misses (an indexed class) are worth chasing.
    hist = dict(prior.get("unresolved", {}))
    for q, c in unresolved_quals.items():
        cls = q.rsplit(".", 1)[0] if "." in q else q
        hist[cls] = hist.get(cls, 0) + c
    top_unresolved = sorted(hist.items(), key=lambda kv: -kv[1])[:8]
    data = {"methods": list(methods.values()), "edges": list(edgeset.values()),
            "values": values_out, "mutations": mutations_out, "runs": runs,
            "instrumented_classes": sorted(instrumented), "unresolved": hist}
    os.makedirs(os.path.dirname(_path(repo)), exist_ok=True)
    with open(_path(repo), "w") as _o:
        json.dump(data, _o, indent=2)
    return {"ok": True, "env": eff_env, "methods": len(methods), "edges": len(edgeset),
            "values": len(values_out), "mutations": len(mutations_out), "runs": sorted(runs),
            "unresolved": unresolved, "unresolved_top": top_unresolved}


def load(repo):
    try:
        with open(_path(repo)) as _f:
            return json.load(_f)
    except Exception:
        return {"methods": [], "edges": []}


def confirmed_nodes(idx, repo):
    """Node ids observed executing AND still fresh (the cited method's source is unchanged since the run).
    A method that changed since the trace is no longer 'confirmed' — runtime facts go stale like memory."""
    rg = idx["rg"]; out = set()
    for m in load(repo).get("methods", []):
        if m["anchor"] in rg.nodes:
            cur = MEM._anchor_hash(idx, repo, m["anchor"])
            if cur is not None and cur == m.get("hash"):
                out.add(m["anchor"])
    return out


def call_edges(idx, repo, fresh_only=True):
    """Real (caller, callee, n) edges observed at runtime, both endpoints still present (+ fresh if asked)."""
    rg = idx["rg"]
    conf = confirmed_nodes(idx, repo) if fresh_only else None
    out = []
    for e in load(repo).get("edges", []):
        if e["caller"] in rg.nodes and e["callee"] in rg.nodes:
            if conf is None or (e["caller"] in conf and e["callee"] in conf):
                out.append((e["caller"], e["callee"], e.get("n", 1)))
    return out


def traced_anchors(idx, repo):
    """Every method anchor present in the trace, REGARDLESS of freshness — i.e. 'was ever observed running'.
    Lets the join tell 'observed but the code changed since' (stale) apart from 'never observed' (untested)."""
    rg = idx["rg"]
    return {m["anchor"] for m in load(repo).get("methods", []) if m["anchor"] in rg.nodes}


_ARG_STR = __import__("re").compile(r'"([^"]+)"')


def observed_config_values(idx):
    """The agent-uncatchable config fact: the LIVE value a config key actually resolved to at runtime —
    which is simply the observed RETURN of a config-getter method called with that key. We scan captured
    value samples for `("<key>") => <value>` where <key> is a known config key, so a runtime override
    (consul/env/Properties) that differs from the file value becomes visible. Truthful: observed, not guessed.
    Returns {norm_key: {value, n, envs, anchor}}."""
    cfg = idx.get("config") or {}
    if not cfg:
        return {}
    from .config_index import _norm
    keyset = set(cfg.keys())
    out = {}
    for anchor, samples in (idx.get("rt_values") or {}).items():
        for s in samples:
            v = s.get("v", "")
            if " => " not in v:
                continue
            argpart, ret = v.split(" => ", 1)
            # scan ALL quoted args (a config getter may take the key as a non-first arg, e.g.
            # getProperty(default, key) or get(scope, key)) — the first arg matching a known key wins.
            key = next((_norm(g) for g in _ARG_STR.findall(argpart) if _norm(g) in keyset), None)
            if key is None:
                continue
            rv = ret.strip()
            if rv.startswith('"') and rv.endswith('"'):
                rv = rv[1:-1]
            rec = out.setdefault(key, {"value": rv, "n": 0, "anchor": anchor, "envs": {}})
            rec["n"] += s.get("n", 0)
            for e, c in s.get("envs", {}).items():
                rec["envs"][e] = rec["envs"].get(e, 0) + c
    return out


def attach(idx, repo):
    """Compute the freshness-checked runtime overlay once and stash it on idx for rank/impact/context/explain.
    No-op cost when there's no trace. Sets idx['rt_confirmed'] (fresh, observed node ids), idx['rt_edges']
    (edges among confirmed nodes), and idx['rt_traced'] (ever-observed anchors, ignoring freshness)."""
    if "rt_confirmed" in idx:                        # already attached this load
        return idx
    try:
        conf = confirmed_nodes(idx, repo)
        data = load(repo)
        idx["rt_confirmed"] = conf
        idx["rt_traced"] = traced_anchors(idx, repo)
        idx["rt_edges"] = [e for e in call_edges(idx, repo, fresh_only=False)
                           if e[0] in conf and e[1] in conf]
        # observed values only for FRESH methods — if the code changed, its old values are stale too
        idx["rt_values"] = {a: s for a, s in data.get("values", {}).items() if a in conf}
        idx["rt_runs"] = data.get("runs", {})            # {env: {profile, mode}} — the run/profile registry
        idx["rt_method_envs"] = {m["anchor"]: m.get("envs", {})           # which env(s) each method ran under
                                 for m in data.get("methods", []) if m["anchor"] in conf}
        idx["rt_config_values"] = observed_config_values(idx)            # key -> live observed value
        # observed state mutations; keep those whose writer node is still fresh (or has no resolvable node)
        idx["rt_mutations"] = [m for m in data.get("mutations", [])
                               if not m.get("anchor") or m["anchor"] in conf]
        idx["rt_instrumented"] = set(data.get("instrumented_classes", []))   # for coverage diagnosis
    except Exception:
        idx["rt_confirmed"] = set(); idx["rt_traced"] = set(); idx["rt_edges"] = []
        idx["rt_values"] = {}; idx["rt_runs"] = {}; idx["rt_method_envs"] = {}
        idx["rt_config_values"] = {}; idx["rt_mutations"] = []; idx["rt_instrumented"] = set()
    return idx


def _class_of(qual):
    """The declaring-class qual of a node qual (drop the trailing method segment)."""
    return qual.rsplit(".", 1)[0] if "." in qual else qual


def coverage(idx, repo, node_id):
    """Classify a method for the 'did it miss X / is it a gap?' question:
       executed         — observed running (on a traced path),
       instrumented     — its class WAS instrumented but the method never ran (coverage gap: drive it),
       not-instrumented — its class was never transformed (real gap — check `--debug` for a WARN),
       no-trace         — there's no runtime overlay at all yet."""
    rg = idx["rg"]
    n = rg.nodes.get(node_id)
    if n is None:
        return {"status": "unknown"}
    traced = idx.get("rt_traced") or set()
    instr = idx.get("rt_instrumented") or set()
    me = idx.get("rt_method_envs") or {}
    if not traced and not instr:
        return {"status": "no-trace", "qual": n.qual}
    if node_id in traced:
        envs = sorted(me.get(node_id, {}))
        return {"status": "executed", "qual": n.qual, "envs": envs,
                "fresh": node_id in (idx.get("rt_confirmed") or set())}
    # class instrumented? match the node's declaring class against the instrumented FQNs (suffix-tolerant)
    cls = _class_of(n.qual)
    hit = any(ic == cls or ic.endswith("." + cls) or ic.replace("$", ".").endswith("." + cls) for ic in instr)
    return {"status": "instrumented" if hit else "not-instrumented", "qual": n.qual}
