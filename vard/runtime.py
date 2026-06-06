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


def _resolve_qual(qual2ids, trace_qual):
    """Map a JVM trace qual to a node id. Normalize nested-class `$`→`.`, then try dotted suffixes of the
    FQN from longest to shortest, returning the first (longest, most specific) that hits exactly one node."""
    q = trace_qual.replace("$", ".")
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


def ingest(idx, repo, trace_path):
    """Parse the agent's JSONL trace, resolve method quals to VARD nodes, persist runtime-confirmed
    methods + real call edges (each method anchored with a freshness hash). Accretes: merges with any
    prior runtime.json (a method/edge seen in any run stays confirmed until its code changes)."""
    rg = idx["rg"]
    q2i = _qual_index(rg)
    prior = load(repo)
    methods = {m["anchor"]: m for m in prior.get("methods", []) if m["anchor"] in rg.nodes}
    edgeset = {(e["caller"], e["callee"]): e for e in prior.get("edges", [])
               if e["caller"] in rg.nodes and e["callee"] in rg.nodes}
    valuemap = {a: {s["v"]: int(s["n"]) for s in samples}                 # anchor -> {sample-string: count}
                for a, samples in prior.get("values", {}).items() if a in rg.nodes}
    config = dict(prior.get("config", {}))
    unresolved = 0
    try:
        for line in open(trace_path, encoding="utf-8", errors="ignore"):
            line = line.strip()
            if not line:
                continue
            o = json.loads(line)
            if o.get("t") == "method":
                nid = _resolve_qual(q2i, o["qual"])
                if not nid:
                    unresolved += 1; continue
                n = rg.nodes[nid]
                prev = methods.get(nid, {}).get("hits", 0)
                methods[nid] = {"anchor": nid, "file": n.file, "qual": n.qual,
                                "hits": prev + int(o.get("hits", 0)),
                                "hash": MEM._anchor_hash(idx, repo, nid)}
            elif o.get("t") == "edge":
                ca, ce = _resolve_qual(q2i, o["caller"]), _resolve_qual(q2i, o["callee"])
                if not (ca and ce) or ca == ce:
                    continue
                key = (ca, ce)
                prev = edgeset.get(key, {}).get("n", 0)
                edgeset[key] = {"caller": ca, "callee": ce, "n": prev + int(o.get("n", 1))}
            elif o.get("t") == "value":                                  # observed (args ⇒ ret) samples
                nid = _resolve_qual(q2i, o["qual"])
                if not nid:
                    unresolved += 1; continue
                bucket = valuemap.setdefault(nid, {})
                for s in o.get("samples", []):
                    bucket[s["v"]] = bucket.get(s["v"], 0) + int(s.get("n", 1))
            elif o.get("t") == "config":                                 # trace fingerprint (active profile)
                if o.get("profile"):
                    config["profile"] = o["profile"]
                config["mode"] = o.get("mode", config.get("mode", "sample"))
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}
    # cap stored value samples per method (richest-first) so the overlay can't bloat
    values_out = {a: [{"v": v, "n": n} for v, n in sorted(b.items(), key=lambda kv: -kv[1])[:8]]
                  for a, b in valuemap.items()}
    data = {"methods": list(methods.values()), "edges": list(edgeset.values()),
            "values": values_out, "config": config}
    os.makedirs(os.path.dirname(_path(repo)), exist_ok=True)
    json.dump(data, open(_path(repo), "w"), indent=2)
    return {"ok": True, "methods": len(methods), "edges": len(edgeset),
            "values": len(values_out), "unresolved": unresolved}


def load(repo):
    try:
        return json.load(open(_path(repo)))
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
        idx["rt_config"] = data.get("config", {})        # trace fingerprint (active profile, capture mode)
    except Exception:
        idx["rt_confirmed"] = set(); idx["rt_traced"] = set(); idx["rt_edges"] = []
        idx["rt_values"] = {}; idx["rt_config"] = {}
    return idx
