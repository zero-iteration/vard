#!/usr/bin/env python3
"""Field-sensitive value-flow edges (SVF-inspired; idea 2 from the research synthesis).

v1 coupled on cache/queue/table; v2 coupled on whole TYPES (over-connected big repos). This couples on
the specific FIELD of a type: a file that WRITES T.f (x.setF(...) / x.f = ...) is coupled to a file that
READS T.f (x.getF()/x.isF()/x.f). Field-level is far sparser than type-level -> should fix the large-repo
over-connection while keeping the real producer<->consumer coupling. Approximate value-flow (no points-to):
scoped to data/entity types a node references. New module; nothing in vard/ touched.

  python -m eval.valueflow <repo>
"""
import sys, os, re, collections
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vard import cli, state as ST
from eval import coupling_compare as CC, edges2 as E2

_FIELD = re.compile(r'(?:private|protected|public)\s+(?:final\s+)?(?:static\s+)?[\w<>,\[\].]+\s+([a-z]\w*)\s*[;=]')


def fields_of_types(sg, rg, repo, cache):
    """data/entity type -> set(field names) declared in its class body."""
    out = {}
    for t, dids in sg["type_def"].items():
        if not E2._is_data_type(sg, rg, t):
            continue
        fs = set()
        for did in dids:
            fs |= set(_FIELD.findall(ST._node_text(repo, rg.nodes[did], cache)))
        fs = {f for f in fs if len(f) >= 3 and f not in ("this", "args", "log", "logger")}
        if fs:
            out[t] = fs
    return out


def field_resource_index(idx, repo):
    rg = idx["rg"]
    sg = idx.get("state") or ST.build_state_graph(rg, os.path.abspath(repo))
    cache = {}
    tf = fields_of_types(sg, rg, repo, cache)
    R = collections.defaultdict(lambda: collections.defaultdict(set))
    for t, fields in tf.items():
        for nid in sg["type_refs"].get(t, []):
            n = rg.nodes.get(nid)
            if not n:
                continue
            txt = ST._node_text(repo, n, cache)
            for field in fields:
                F = field[0].upper() + field[1:]
                w = (f".set{F}(" in txt) or bool(re.search(r'\.' + re.escape(field) + r'\s*=[^=]', txt))
                r = (f".get{F}(" in txt) or (f".is{F}(" in txt) or bool(re.search(r'\.' + re.escape(field) + r'\b(?!\s*=)', txt))
                if not (w or r):
                    continue
                rid = f"field:{t}.{field}"
                R[rid][n.file].add("w" if w else "r")
    return {rid: dict(fm) for rid, fm in R.items() if len(fm) >= 2}


def main():
    repo = cli._project_root(sys.argv[1]) if len(sys.argv) > 1 else "."
    idx = cli.fresh_index(repo)
    print(f"repo={os.path.basename(repo)}")
    E2._eval(CC.resource_index(idx), repo, "v1  (cache/queue/table)")
    E2._eval(E2.resource_index_v2(idx, repo), repo, "v2  (whole types)")
    E2._eval(field_resource_index(idx, repo), repo, "vf  (field-sensitive value-flow)")


if __name__ == "__main__":
    main()
