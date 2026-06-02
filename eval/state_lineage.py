"""State-lineage retriever (v1) — the state-first idea, scoped to what Run 3 validated.

Spine (one mechanism for every pattern): a *resource* (cache key / message topic / in-memory
holder) implies a STATE TYPE. The lineage of that type = {its definition} + {producers/consumers
that reference it} + {interface/impl of it}. The query implicates resources (by kind + relevance);
we return their type lineages.

State-type extraction differs by where the payload lives:
  - cache:  the writer's RETURN TYPE  (Result<XxxCO> -> Result, XxxCO) — annotation args excluded
  - queue:  types referenced in the listener BODY (the payload is cast there, not in the signature)
  - in-mem: the holder class + its field types

Validated patterns (eval/FINDINGS.md Run 3). Reuses VARD's rg + node_decorators + inherits edges
plus on-disk source. Plugged into retrievers.REGISTRY as `state_lineage`.
"""
import os, re
from vard import rank as RK, selflabel as SL, common as C
from . import channels as CH

_CAP = re.compile(r'\b([A-Z][A-Za-z0-9_]+)\b')
_CACHE_ANN = re.compile(r'\b(DataCache|Cacheable|CachePut|CacheEvict)\b')
_QUEUE_ANN = re.compile(r'\b(KafkaListener|RabbitListener|RocketMQMessageListener|EventListener|TransactionalEventListener)\b')
# repo types that are infra/scaffolding, never the "state" — excluded as lineage anchors
_INFRA = re.compile(r'(Constants?|OperateType|TraceLog|Mapper|Service|ServiceI|Controller|Repository|'
                    r'Config|Utils?|Factory|Exception|Test|Application|Aspect|Filter|Builder|'
                    r'Cmd|CmdExe|Qry|Gateway|GatewayImpl|Properties|Enum)$')
_CACHE_Q = re.compile(r'\b(cache|cached|caching|redis|seriali|deserciali|deserial|jackson|round-?trip|wrapper)\b', re.I)
_QUEUE_Q = re.compile(r'\b(kafka|queue|topic|consum|publish|listener|message|event|broker|rabbit|rocket)\b', re.I)
_MAX_REFS = 40                          # a type referenced by >this many nodes is infra, not state
# a real in-memory state holder has a static MUTABLE field (not final, not a logger, not a CONSTANT).
# Matching `static <Type> <name> =|;` (a field decl) — NOT `static void main(` etc., which flagged 314 classes.
_STATIC_FIELD = re.compile(r'\bstatic\s+(?!final\b)([A-Za-z_][\w.<>\[\]]*)\s+([A-Za-z_]\w*)\s*[=;]')
_LOGGER = re.compile(r'(?:Logger|Logger<.*>|^Log|Slf4j)$')


def _src(repo, rel, cache):
    if rel not in cache:
        try:
            cache[rel] = open(os.path.join(repo, rel), encoding="utf-8", errors="ignore").read().splitlines()
        except Exception:
            cache[rel] = []
    return cache[rel]


def _node_text(repo, n, cache):
    return "\n".join(_src(repo, n.file, cache)[n.start - 1:n.end])


def _return_type_types(repo, n, cache, typenames):
    """Repo types in the method's RETURN TYPE (e.g. Result<DeptCO> -> {Result, DeptCO}). node.start
    can point at a leading annotation, so scan a small window for the `<type> name(` declaration."""
    decl = " ".join(_src(repo, n.file, cache)[n.start - 1:n.start + 8])
    m = re.search(r'([A-Za-z_][\w.<>,\[\]\s]*?)\s+' + re.escape(n.name) + r'\s*\(', decl)
    if not m:
        return set()
    return {t for t in _CAP.findall(m.group(1)) if t in typenames}


def build_state_graph(idx, repo):
    rg = idx["rg"]
    content = CH.content_nodes(rg)
    type_def = {}
    for n in rg.nodes.values():
        if n.type == "class":
            type_def.setdefault(n.name, []).append(n.id)
    typenames = set(type_def)

    up, down = {}, {}
    for u, v, k in rg.G.edges(keys=True):
        if k == "inherits":
            up.setdefault(u, set()).add(v)
            down.setdefault(v, set()).add(u)

    cache = {}
    type_refs = {}
    deco = getattr(rg, "node_decorators", {})
    resources = []                                      # (node_id, kind, state_types:set)
    holders = {}

    for n in content:
        txt = _node_text(repo, n, cache)
        refs = {t for t in _CAP.findall(txt) if t in typenames and t != n.name}
        for t in refs:
            type_refs.setdefault(t, set()).add(n.id)

        anns = " ".join(deco.get(n.id, []))
        if _CACHE_ANN.search(anns):
            st = {t for t in _return_type_types(repo, n, cache, typenames) if not _INFRA.search(t)}
            resources.append((n.id, "cache", st))
        elif _QUEUE_ANN.search(anns):
            st = {t for t in refs if not _INFRA.search(t)}
            resources.append((n.id, "queue", st))

        if n.type == "class":
            real = [(ty, nm) for ty, nm in _STATIC_FIELD.findall(txt)
                    if not _LOGGER.search(ty) and not nm.isupper()]   # drop loggers + CONSTANTS
            if real:
                ftypes = {ty for ty, _ in real if ty in typenames and not _INFRA.search(ty)}
                holders[n.id] = ftypes | {n.name}

    return {"type_def": type_def, "type_refs": type_refs, "up": up, "down": down,
            "resources": resources, "holders": holders, "content": content, "typenames": typenames}


def state_file_edges(idx, repo, sg=None):
    """State-coupling edges (file level) to MERGE into VARD's propagation graph: a writer file is
    linked to the files that DEFINE its state shapes, and those defs to the files that produce/consume
    them. Lets content relevance propagate through shared state the same way it flows through imports.
    Generic infra shapes (refs > _MAX_REFS, e.g. Result) link only writer<->def, never to all referers."""
    rg = idx["rg"]
    sg = sg or build_state_graph(idx, repo)
    fof = lambda nid: rg.nodes[nid].file
    edges = set()

    def link_type(anchor_file, t):
        defs = sg["type_def"].get(t, [])
        for did in defs:
            edges.add((anchor_file, fof(did)))
        refs = sg["type_refs"].get(t, set())
        if len(refs) <= _MAX_REFS:                      # don't fan a generic wrapper out to the whole repo
            for r in refs:
                for did in defs:
                    edges.add((fof(did), fof(r)))

    for nid, kind, stypes in sg["resources"]:
        for t in stypes:
            link_type(fof(nid), t)
    for hid, stypes in sg["holders"].items():
        for t in stypes:
            link_type(fof(hid), t)
    return [(a, b) for a, b in edges if a != b]


def _type_closure(sg, typename, rg):
    out = set()
    for did in sg["type_def"].get(typename, []):
        out.add(did)
        out |= sg["up"].get(did, set()) | sg["down"].get(did, set())   # interface<->impl, super<->sub
        out |= {v for _, v, k in rg.G.out_edges(did, keys=True) if k == "contains"}  # the type's own members (ctor, getters)
    refs = sg["type_refs"].get(typename, set())
    if len(refs) <= _MAX_REFS:                          # generic infra types: keep the def, drop the refs
        out |= refs
    return out


def gated_state_closure(idx, task, repo, seeds, return_meta=False):
    """Typed-traversal closure that FIRES ONLY WHEN a state resource is implicated — the state half of
    the unified retriever. No broad seed-reference fallback (that was the logic-bug noise). Returns
    {} on a pure logic bug (no cache/queue kind in the query, no resource near the seeds, no holder
    near the seeds), so the hybrid collapses to plain content ranking there."""
    rg = idx["rg"]
    sg = build_state_graph(idx, repo)
    seedset = set(seeds)
    seed_files = {rg.nodes[i].file for i in seeds}
    qtok = set(C.subtokens(task))
    kinds = set()
    if _CACHE_Q.search(task): kinds.add("cache")
    if _QUEUE_Q.search(task): kinds.add("queue")

    implicated = set()                                  # state types to expand
    for nid, kind, stypes in sg["resources"]:
        n = rg.nodes[nid]
        if kind in kinds or nid in seedset or n.file in seed_files or (qtok & set(C.subtokens(n.qual))):
            implicated |= stypes
    for hid, stypes in sg["holders"].items():           # in-memory holder, only if the search landed on it
        if hid in seedset or rg.nodes[hid].file in seed_files:
            implicated |= stypes

    closure = set()
    for t in implicated:
        closure |= _type_closure(sg, t, rg)
    if return_meta:
        return closure, {"kinds": sorted(kinds), "n_implicated_types": len(implicated),
                         "n_holders": len(sg["holders"]), "closure_size": len(closure)}
    return closure


def general_state_closure(idx, task, repo, seeds, return_meta=False):
    """GENERAL state model (the corrected thesis): state = ALL types, not just cache/queue resources.
    Implicated state = types NAMED in the symptom + types referenced by the content seeds. Closure =
    each implicated type's def + members + producers/consumers (refs) + interface/impl. Coupling,
    plugin-registry, entity-DTO and cache all collapse to one mechanism: a type produced here, used
    there. No resource whitelist."""
    rg = idx["rg"]
    sg = build_state_graph(idx, repo)
    seedset = set(seeds)
    seed_files = {rg.nodes[i].file for i in seeds}
    qtok = set(C.subtokens(task))
    idents, _ = C.extract_seeds(task)

    implicated = {}
    def bump(t, a): implicated[t] = implicated.get(t, 0.0) + a
    # (1) types NAMED in the symptom (strongest signal — e.g. DoNotMockEnforcer)
    for t in idents:
        if t in sg["type_def"] and not _INFRA.search(t):
            bump(t, 3.0)
    # (2) types referenced by the content seeds (bridge when the wrong state isn't named outright)
    for t, refs in sg["type_refs"].items():
        if refs & seedset and not _INFRA.search(t):
            bump(t, 1.0 + 0.5 * len(qtok & set(C.subtokens(t))))
    # (3) resources are not the DEFINITION of state, but a strong HINT for which state is implicated:
    #     a cache writer / queue listener near the query points at its payload type as the wrong state.
    kinds = set()
    if _CACHE_Q.search(task): kinds.add("cache")
    if _QUEUE_Q.search(task): kinds.add("queue")
    for nid, kind, stypes in sg["resources"]:
        n = rg.nodes[nid]
        if kind in kinds or nid in seedset or n.file in seed_files or (qtok & set(C.subtokens(n.qual))):
            for t in stypes:
                bump(t, 2.0)
    # keep it bounded: the most-implicated state types only
    top = [t for t in sorted(implicated, key=implicated.get, reverse=True)[:8]]
    closure = set()
    for t in top:
        closure |= _type_closure(sg, t, rg)
    if return_meta:
        return closure, {"implicated": top, "closure_size": len(closure)}
    return closure


def retrieve(idx, task, repo, k=8, return_meta=False):
    rg = idx["rg"]
    sg = build_state_graph(idx, repo)
    score, _ = RK.rank_nodes(idx, task, repo, sg["content"], weights=SL.load_weights(repo))
    seeds = sorted(score, key=score.get, reverse=True)[:k]
    seedset, seed_files = set(seeds), {rg.nodes[i].file for i in seeds}
    qtok = set(C.subtokens(task))

    kinds = set()
    if _CACHE_Q.search(task): kinds.add("cache")
    if _QUEUE_Q.search(task): kinds.add("queue")

    # PRIMARY state = the shapes carried by resources the query implicates. When the query names a
    # mechanism (cache/queue), every resource of that kind is implicated; otherwise only resources
    # near the seeds or matching the query by name. These shapes are THE state — expand all of them.
    primary = set()
    for nid, kind, stypes in sg["resources"]:
        n = rg.nodes[nid]
        if kind in kinds or nid in seedset or n.file in seed_files or (qtok & set(C.subtokens(n.qual))):
            primary |= stypes
    # SECONDARY (supplement, capped): types the seeds reference + in-memory holders near the seeds.
    sec = {}
    def bump(t, a): sec[t] = sec.get(t, 0.0) + a
    for t, refs in sg["type_refs"].items():
        if refs & seedset and not _INFRA.search(t) and t not in primary:
            bump(t, 1.0 + 0.4 * len(qtok & set(C.subtokens(t))))
    for hid, stypes in sg["holders"].items():
        if hid in seedset or rg.nodes[hid].file in seed_files:
            for t in stypes:
                if t not in primary: bump(t, 1.6)
    secondary = sorted(sec, key=sec.get, reverse=True)[:6]

    closure = set(seeds)
    for t in primary | set(secondary):
        closure |= _type_closure(sg, t, rg)

    if return_meta:
        return closure, {"kinds": sorted(kinds), "primary": sorted(primary), "secondary": secondary,
                         "n_resources": len(sg["resources"]), "closure_size": len(closure)}
    return closure
