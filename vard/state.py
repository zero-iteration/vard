#!/usr/bin/env python3
"""State-first localization layer.

The program's STATE (types/fields — the data it's about) is the skeleton; code hangs off it as
"what produces / consumes / mutates this state". To localize a task: identify the implicated state,
then return the code that defines and produces it. Coupling (a state written by more than one place)
falls out as one case — it is not special.

The state graph is built once at index time (build_state_graph, stored in the index) so queries are
instant. Two query paths:
  - auto_section(): zero-shot, GATED — fires only when state is clearly implicated (a type named in
    the task, or a cache/queue resource the task points at). Precise; used by `vard context`.
  - candidates()/lineage(): the agent names the implicated state, VARD traverses it. This is the
    strong path for state whose name the task never mentions (validated: agent identification recovers
    dark state that heuristics/embeddings cannot — see eval/FINDINGS.md).
"""
import os, re

_CAP = re.compile(r'\b([A-Z][A-Za-z0-9_]+)\b')
# repo types that are infra/scaffolding, never the "state" itself
_INFRA = re.compile(r'(Constants?|OperateType|TraceLog|Mapper|Service|ServiceI|Controller|Repository|'
                    r'Config|Utils?|Factory|Exception|Test|Application|Aspect|Filter|Builder|'
                    r'Cmd|CmdExe|Qry|Gateway|GatewayImpl|Properties|Enum)$')
_CACHE_ANN = re.compile(r'\b(DataCache|Cacheable|CachePut|CacheEvict)\b')
_QUEUE_ANN = re.compile(r'\b(KafkaListener|RabbitListener|RocketMQMessageListener|EventListener|TransactionalEventListener)\b')
_CACHE_Q = re.compile(r'\b(cache|cached|caching|redis|seriali|deserial|jackson|round-?trip|wrapper)\b', re.I)
_QUEUE_Q = re.compile(r'\b(kafka|queue|topic|consum|publish|listener|message|event|broker|rabbit|rocket)\b', re.I)
_STATIC_FIELD = re.compile(r'\bstatic\s+(?!final\b)([A-Za-z_][\w.<>\[\]]*)\s+([A-Za-z_]\w*)\s*[=;]')
_LOGGER = re.compile(r'(?:Logger|Logger<.*>|^Log|Slf4j)$')
_MAX_REFS = 40


def _content_nodes(rg):
    return [n for n in rg.nodes.values() if n.type in ("function", "method", "class")]


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
    decl = " ".join(_src(repo, n.file, cache)[n.start - 1:n.start + 8])
    m = re.search(r'([A-Za-z_][\w.<>,\[\]\s]*?)\s+' + re.escape(n.name) + r'\s*\(', decl)
    return {t for t in _CAP.findall(m.group(1)) if t in typenames} if m else set()


def build_state_graph(rg, repo):
    """Compute the state graph from the symbol graph + on-disk source. Stored in the index.
    Returns a picklable dict: type_def, type_refs, up/down (interface<->impl), resources, holders."""
    content = _content_nodes(rg)
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

    cache, type_refs, resources, holders = {}, {}, [], {}
    deco = getattr(rg, "node_decorators", {})
    for n in content:
        txt = _node_text(repo, n, cache)
        refs = {t for t in _CAP.findall(txt) if t in typenames and t != n.name}
        for t in refs:
            type_refs.setdefault(t, set()).add(n.id)
        anns = " ".join(deco.get(n.id, []))
        if _CACHE_ANN.search(anns):
            st = {t for t in _return_type_types(repo, n, cache, typenames) if not _INFRA.search(t)}
            resources.append((n.id, "cache", sorted(st)))
        elif _QUEUE_ANN.search(anns):
            resources.append((n.id, "queue", sorted({t for t in refs if not _INFRA.search(t)})))
        if n.type == "class":
            real = [(ty, nm) for ty, nm in _STATIC_FIELD.findall(txt)
                    if not _LOGGER.search(ty) and not nm.isupper()]
            if real:
                holders[n.id] = sorted({ty for ty, _ in real if ty in typenames and not _INFRA.search(ty)} | {n.name})
    # store sets as sorted lists for clean pickling
    return {"type_def": type_def,
            "type_refs": {t: sorted(ids) for t, ids in type_refs.items()},
            "up": {k: sorted(v) for k, v in up.items()},
            "down": {k: sorted(v) for k, v in down.items()},
            "resources": resources, "holders": holders}


# ---- query side -------------------------------------------------------------

def _type_closure(sg, name, rg):
    out = set()
    for did in sg["type_def"].get(name, []):
        out.add(did)
        out |= set(sg["up"].get(did, [])) | set(sg["down"].get(did, []))
        out |= {v for _, v, k in rg.G.out_edges(did, keys=True) if k == "contains"}
    refs = sg["type_refs"].get(name, [])
    if len(refs) <= _MAX_REFS:
        out |= set(refs)
    return out


def lineage(sg, rg, type_names):
    """Traverse the named state types -> their def + members + producers/consumers + interface/impl."""
    ids = set()
    for t in type_names:
        if t in sg["type_def"]:
            ids |= _type_closure(sg, t, rg)
    return ids


def candidates(sg, rg, seed_files=None, task="", max_n=400):
    """The state types the agent chooses from. For big repos, narrowed to types whose def lives in or
    near (1 import hop) the files the search surfaced — structural, so textually-disconnected state is
    kept. seed_files: files of the content-top hits."""
    names = sorted({t for t in sg["type_def"] if not _INFRA.search(t)})
    if len(names) <= max_n or not seed_files:
        return names[:max_n]
    near = set()
    for t in names:
        for did in sg["type_def"].get(t, []):
            if rg.nodes[did].file in seed_files:
                near.add(t)
    return sorted(near)[:max_n]


def auto_implicated(sg, rg, task, seed_ids):
    """GATED implication for zero-shot use: types NAMED in the task + state shapes of cache/queue
    resources the task points at. Deliberately omits the broad seed-reference path (it adds noise on
    ordinary bugs); the agent path covers state the task doesn't name."""
    from . import common as C
    idents, _ = C.extract_seeds(task)
    qtok = set(C.subtokens(task))
    seedset, seed_files = set(seed_ids), {rg.nodes[i].file for i in seed_ids}
    out = set()
    for t in idents:
        if t in sg["type_def"] and not _INFRA.search(t):
            out.add(t)
    kinds = set()
    if _CACHE_Q.search(task or ""):
        kinds.add("cache")
    if _QUEUE_Q.search(task or ""):
        kinds.add("queue")
    for nid, kind, stypes in sg["resources"]:
        n = rg.nodes.get(nid)
        if n is None:
            continue
        if kind in kinds or nid in seedset or n.file in seed_files or (qtok & set(C.subtokens(n.qual))):
            out |= set(stypes)
    for hid, stypes in sg["holders"].items():
        if hid in seedset or rg.nodes.get(hid) and rg.nodes[hid].file in seed_files:
            out |= set(stypes)
    return out


def render(rg, ids, exclude=None):
    exclude = exclude or set()
    lines = []
    for nid in sorted(ids):
        if nid in exclude or nid not in rg.nodes:
            continue
        n = rg.nodes[nid]
        lines.append(f"- {n.file}:{n.start}-{n.end}  {n.qual}")
    return lines
