#!/usr/bin/env python3
"""Query layer over a built index: impact (blast radius), resource lookup, trace, and
coupling reasons. Pure functions on the index dict {rg, res} — no re-indexing."""
import os
from collections import defaultdict


def _writes_reads_by_fn(res):
    """fn id -> ({resources it writes}, {resources it reads})."""
    w, r = defaultdict(set), defaultdict(set)
    for rid in res["nodes"]:
        for f in res["writers"].get(rid, []):
            w[f].add(rid)
        for f in res["readers"].get(rid, []):
            r[f].add(rid)
    return w, r


def resolve_target(idx, target):
    """Resolve a user string to node id(s): full id, file:line, qualified name, or bare name."""
    rg = idx["rg"]
    t = target.strip()
    if "::" in t and t in rg.nodes:
        return [t]
    # file:line
    if ":" in t and t.rsplit(":", 1)[-1].isdigit():
        f, ln = t.rsplit(":", 1); ln = int(ln); f = f.lstrip("./")
        hits = [n for n in rg.by_file.get(f, []) if n.start <= ln <= n.end and n.type != "module"]
        hits.sort(key=lambda n: n.end - n.start)
        return [hits[0].id] if hits else []
    exact = [nid for nid, n in rg.nodes.items() if n.qual == t]
    if exact:
        return exact
    suf = [nid for nid, n in rg.nodes.items() if n.qual.endswith("." + t)]
    if suf:
        return suf[:8]
    nm = [nid for nid, n in rg.nodes.items() if n.name == t and n.type != "module"]
    return nm[:8]


def _loc(n):
    return f"{n.file}:{n.start}-{n.end}"


def impact(idx, target, limit=40):
    """Blast radius for a target symbol: coupled functions through shared resources +
    structural neighbours + (name-based) callers, each with a reason."""
    rg, res = idx["rg"], idx["res"]
    ids = resolve_target(idx, target)
    if not ids:
        return {"target": target, "error": "symbol not found", "items": []}
    wbyf, rbyf = _writes_reads_by_fn(res)
    items, seen = [], set()

    def add(nid, relation, reason, rid=None):
        if nid in ids or nid in seen or nid not in rg.nodes or nid.endswith("<module>"):
            return
        seen.add(nid); n = rg.nodes[nid]
        items.append({"qual": n.qual, "loc": _loc(n), "relation": relation, "via": rid, "reason": reason})

    target_writes, target_reads = set(), set()
    for tid in ids:
        target_writes |= wbyf.get(tid, set())
        target_reads |= rbyf.get(tid, set())

    # downstream: this WRITES a resource that others READ  (they depend on what you write)
    for rid in target_writes:
        kind = rid.split(":", 1)[0]
        verb = "consume the queue" if kind == "queue" else f"read {rid}"
        for nid in res["readers"].get(rid, []):
            add(nid, "downstream", f"{verb} that this writes — update if the value's shape/meaning/timing changes", rid)
    # upstream: this READS a resource others WRITE  (you depend on them)
    for rid in target_reads:
        kind = rid.split(":", 1)[0]
        verb = "produce the queue" if kind == "queue" else f"write {rid}"
        for nid in res["writers"].get(rid, []):
            add(nid, "upstream", f"{verb} that this reads — this behaviour depends on them", rid)
    # co-writers (consistency)
    for rid in target_writes:
        for nid in res["writers"].get(rid, []):
            add(nid, "co-writer", f"also writes {rid} — keep writes consistent", rid)

    # structural: same-class / same-module siblings via the contains edge
    for tid in ids:
        for p, _, k in rg.G.in_edges(tid, keys=True):
            if k == "contains":
                for _, sib, k2 in rg.G.out_edges(p, keys=True):
                    if k2 == "contains":
                        add(sib, "sibling", f"same class/module as the target", None)

    # callers by method name (approx; product graph has no resolved call edges)
    names = {rg.nodes[tid].name for tid in ids}
    callers = defaultdict(int); caller_loc = {}
    for cs in getattr(rg, "call_sites", []):
        if cs.method in names:
            cid = f"{cs.file}::{cs.enclosing_qual}"
            if cid in rg.nodes and cid not in ids:
                callers[cid] += 1
    for cid in list(callers)[:20]:
        add(cid, "caller?", f"calls a method named '{'/'.join(names)}' ({callers[cid]}x) — likely a caller (name-based)", None)

    order = {"downstream": 0, "upstream": 1, "co-writer": 2, "sibling": 3, "caller?": 4}
    items.sort(key=lambda x: order.get(x["relation"], 9))
    return {"target": [rg.nodes[i].qual for i in ids],
            "writes": sorted(target_writes), "reads": sorted(target_reads),
            "items": items[:limit], "n": len(items)}


def resource(idx, name, limit=60):
    rg, res = idx["rg"], idx["res"]
    q = name.strip().lower()
    matches = [rid for rid in res["nodes"] if q in rid.lower()]
    out = []
    for rid in matches[:limit]:
        ws = [rg.nodes[f] for f in res["writers"].get(rid, []) if f in rg.nodes]
        rs = [rg.nodes[f] for f in res["readers"].get(rid, []) if f in rg.nodes]
        out.append({"resource": rid,
                    "writers": [{"qual": n.qual, "loc": _loc(n)} for n in ws],
                    "readers": [{"qual": n.qual, "loc": _loc(n)} for n in rs]})
    return {"query": name, "resources": out, "n_matched": len(matches)}


def coupling_reason(idx, task_node_id, partner_id, rid):
    """One-line reason a partner is coupled to a node (used to enrich context)."""
    wbyf, rbyf = _writes_reads_by_fn(idx["res"])
    kind = rid.split(":", 1)[0]
    noun = "queue" if kind == "queue" else rid
    if rid in wbyf.get(task_node_id, set()) and rid in rbyf.get(partner_id, set()):
        return f"reads {noun} that the focus writes"
    if rid in rbyf.get(task_node_id, set()) and rid in wbyf.get(partner_id, set()):
        return f"writes {noun} that the focus reads"
    return f"also touches {noun}"
